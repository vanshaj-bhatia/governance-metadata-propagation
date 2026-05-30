import sys
import os
import logging
import pandas as pd
from typing import List, Dict, Any, Optional

# Add paths
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../dataplex_integration')))

from google.adk.plugins.base_plugin import BasePlugin
from google.cloud import bigquery, dataplex_v1
from glossary_management import GlossaryClient
from similarity_engine import SimilarityEngine
from context import get_credentials, get_oauth_token
from lineage_propagation import LineageGraphTraverser
from doc_description_plugin import DocDescriptionPlugin

logger = logging.getLogger(__name__)

class GlossaryPlugin(BasePlugin):
    def __init__(self, project_id: str, location: str = "europe-west1"):
        super().__init__(name="glossary_plugin")
        self.project_id = project_id
        self.location = location
        self._glossary_client = None
        self._similarity_engine = None
        self._bq_client = None
        self._lineage_traverser = None
        self._link_check_cache = {} # Cache for _check_link_exists: (dataset, table, col, term) -> bool

    def _ensure_initialized(self):
        creds = get_credentials(self.project_id)
        if not self._glossary_client:
            self._glossary_client = GlossaryClient(self.project_id, self.location, credentials=creds)
        if not self._similarity_engine:
            # Vertex AI models are best supported in us-central1 for now
            self._similarity_engine = SimilarityEngine(self.project_id, location="us-central1", credentials=creds)
        if not self._bq_client:
            self._bq_client = bigquery.Client(project=self.project_id, credentials=creds)
        if not self._lineage_traverser:
            token = get_oauth_token()
            self._lineage_traverser = LineageGraphTraverser(self.project_id, self.location, token=token)

    def _cache_term_embeddings(self, all_terms: List[Dict[str, Any]]):
        """Pre-calculates and caches embeddings for all glossary terms."""
        if not self._similarity_engine.embedder:
            return

        texts_to_embed = []
        term_ids = []
        for term in all_terms:
            if term['name'] not in self._similarity_engine.term_embeddings:
                # Combine name and description for a richer semantic representation
                text = f"{term['display_name']}: {term.get('description', '')}"
                texts_to_embed.append(text)
                term_ids.append(term['name'])
        
        if texts_to_embed:
            logger.info(f"Generating embeddings for {len(texts_to_embed)} glossary terms...")
            embs = self._similarity_engine.embedder.get_embeddings(texts_to_embed)
            new_cache = {term_ids[i]: embs[i] for i in range(len(embs))}
            self._similarity_engine.term_embeddings.update(new_cache)

    def _check_link_exists(self, dataset_id: str, table_id: str, col_name: str, term_id: str) -> bool:
        """Checks if a specific term is already linked to a column using deterministic IDs."""

        # 1. Check cache first
        cache_key = (dataset_id, table_id, col_name, term_id)
        if cache_key in self._link_check_cache:
            return self._link_check_cache[cache_key]

        client = dataplex_v1.CatalogServiceClient(credentials=get_credentials(self.project_id))
        
        # Consistent with apply_terms ID construction
        clean_column = col_name.replace("_", "-").lower()
        clean_table = table_id.replace("_", "-").lower()
        entry_link_id = f"link-{clean_table}-{clean_column}"
        
        parent = f"projects/{self.project_id}/locations/{self.location}/entryGroups/@bigquery"
        link_name = f"{parent}/entryLinks/{entry_link_id}"
        
        try:
            link = client.get_entry_link(name=link_name)
            # Verify it's the SAME term (optional but safer)
            target_ref = next((r for r in link.entry_references if r.type_ == dataplex_v1.EntryLink.EntryReference.Type.TARGET), None)
            if target_ref:
                # Comparison: Term resource names might differ by project ID vs Number
                # We check if the unique term identifier (last segment) matches
                target_term_id = target_ref.name.split('/')[-1]
                source_term_id = term_id.split('/')[-1]
                if target_term_id == source_term_id:
                    self._link_check_cache[cache_key] = True
                    return True
        except Exception:
            # Fallback for UI-created links or restricted list permissions.
            # If we had list_entry_links permissions, we'd use it here.
            pass
        
        # We return False if no direct deterministic link is found. 
        # The caller (recommend_terms_for_table) will perform a strict similarity fallback.
        self._link_check_cache[cache_key] = False
        return False

    def _is_column_linked(self, dataset_id: str, table_id: str, col_name: str) -> bool:
        """Checks if a column has ANY glossary term linked to it using deterministic IDs."""
        
        # We check for the deterministic ID used in apply_terms
        client = dataplex_v1.CatalogServiceClient(credentials=get_credentials(self.project_id))
        
        clean_column = col_name.replace("_", "-").lower()
        clean_table = table_id.replace("_", "-").lower()
        entry_link_id = f"link-{clean_table}-{clean_column}"
        
        parent = f"projects/{self.project_id}/locations/{self.location}/entryGroups/@bigquery"
        link_name = f"{parent}/entryLinks/{entry_link_id}"
        
        try:
            client.get_entry_link(name=link_name)
            return True
        except Exception:
            return False

    def recommend_terms_for_table(self, dataset_id: str, table_id: str, doc_path: Optional[List[str]] = None, context_mode: str = "rag", datastore_id: Optional[str] = None) -> pd.DataFrame:
        """
        Fetches recommendations for all columns in a table using Vertex AI Embeddings and Documents.
        """
        self._ensure_initialized()
        table_ref = f"{self.project_id}.{dataset_id}.{table_id}"
        table = self._bq_client.get_table(table_ref)
        
        all_terms = self._glossary_client.get_all_terms()
        if not all_terms:
            logger.warning("No glossary terms found to recommend.")
            return pd.DataFrame()

        # Initialize DocDescriptionPlugin if documents are provided
        doc_plugin = None
        if doc_path:
            logger.info(f"Initializing DocDescriptionPlugin for Glossary in mode: {context_mode}")
            doc_plugin = DocDescriptionPlugin(self.project_id, self.location)
            doc_plugin.load_document(doc_path, mode=context_mode, datastore_id=datastore_id)

        # 1. Warm up Term Cache
        self._cache_term_embeddings(all_terms)

        # 2. Batch Generate Column Embeddings
        col_metas = []
        col_texts = []
        for field in table.schema:
            meta = {
                "name": field.name,
                "description": field.description or "",
                "type": field.field_type
            }
            col_metas.append(meta)
            # Use name and description for column semantic context
            col_texts.append(f"{field.name}: {field.description or ''}")

        col_embeddings = []
        if self._similarity_engine.embedder:
            logger.info(f"Generating batch embeddings for {len(col_texts)} columns in {table_id}...")
            col_embeddings = self._similarity_engine.embedder.get_embeddings(col_texts, task_type="RETRIEVAL_QUERY")

        # 3. Get Column Lineage (Upstream)
        
        # Get Column Lineage (Upstream - Multi-hop)
        # Entry name for lineage is the BigQuery FQN: bigquery:project.dataset.table
        lineage_fqn = f"bigquery:{self.project_id}.{dataset_id}.{table_id}"
        col_list = [f.name for f in table.schema]
        upstream_lineage = self._lineage_traverser.get_recursive_column_lineage(lineage_fqn, col_list)

        # 4. Get Recommendations
        recommendations = []
        for i, col_meta in enumerate(col_metas):
            col_name = col_meta['name']
            col_path = f"Schema.{col_name}"
            col_emb = col_embeddings[i] if i < len(col_embeddings) else None
            
            # Recommendations will check for existing links using _check_link_exists
            
            # A. Lineage-Based Recommendations (Multi-hop)
            lineage_hops = upstream_lineage.get(col_name, [])
            for hop in lineage_hops:
                try:
                    src_entity = hop['source_entity'] # expected project.dataset.table
                    src_col = hop['source_column']
                    hop_confidence = hop['confidence']
                    
                    # SEMANTIC GUARD: If this lineage link was penalized (e.g. Category vs Amount),
                    # we do NOT want to propagate glossary terms through it, though it stays
                    # for structural/description propagation.
                    if hop.get('semantic_penalty'):
                        continue
                    
                    # Robust parsing of project.dataset.table
                    clean_entity = src_entity.replace("bigquery:", "")
                    parts = clean_entity.split('.')
                    if len(parts) >= 3:
                        src_dataset = parts[-2]
                        src_table = parts[-1]

                        # ENRICHMENT: Fetch upstream column description to improve semantic matching
                        src_description = ""
                        try:
                            src_table_ref = f"{self.project_id}.{src_dataset}.{src_table}"
                            if not hasattr(self, '_table_cache'): self._table_cache = {}
                            if src_table_ref not in self._table_cache:
                                self._table_cache[src_table_ref] = self._bq_client.get_table(src_table_ref)
                            
                            target_field = next((f for f in self._table_cache[src_table_ref].schema if f.name == src_col), None)
                            if target_field:
                                src_description = target_field.description or ""
                        except Exception:
                            pass

                        # HEURISTIC: Check if any of our known terms are linked upstream to this column
                        for term in all_terms:
                            term_id = term['name']
                            found_link = self._check_link_exists(src_dataset, src_table, src_col, term_id)
                            rationale = f"Propagated via Lineage from {src_entity}"
                            
                            # FALLBACK: If no direct link is detected (e.g., UI-created links),
                            # we ONLY propagate if the term is an extremely strong match for the upstream column
                            # (> 0.95), effectively a near-exact match. This prevents false positives
                            # on columns that happen to have lineage but no actual term association.
                            if not found_link:
                                upstream_signals = self._similarity_engine.calculate_total_score(
                                    {"name": src_col, "description": src_description, "type": ""}, 
                                    term
                                )
                                
                                # STRICT: Only use lineage rationale if it's almost certainly the same term link
                                # or if the direct match is overwhelming.
                                if upstream_signals['total'] >= 0.95:
                                    found_link = True
                                    rationale = f"Propagated via Lineage (Verified Link)"

                            if found_link:
                                # STRICT CONFIDENCE: Only promote to 1.0 if the lineage mapping itself is strong.
                                # If the mapping is a weak heuristic (< 0.85), we treat it as moderate confidence.
                                final_confidence = 1.0 if hop_confidence >= 0.85 else 0.7
                                recommendations.append({
                                    "Column": col_name,
                                    "Suggested Term": term['display_name'],
                                    "Confidence": final_confidence,
                                    "Rationale": rationale,
                                    "Term ID": term_id 
                                })
                                break # Found a term for this hop
                        
                        if recommendations and recommendations[-1]['Column'] == col_name:
                            break # Already found a term for this column at some hop
                except Exception as e:
                    logger.warning(f"Failed to check upstream glossary links for {col_name} via {hop.get('source_entity')}: {e}")

            if recommendations and recommendations[-1]['Column'] == col_name:
                # Found a lineage-based recommendation for this column!
                # For demo clarity, we prioritize lineage and skip similarity-based suggestions for this column.
                continue

            # B. Document Search
            if doc_plugin:
                allowed_terms = [t['display_name'] for t in all_terms]
                doc_rec = doc_plugin.recommend_glossary_terms_for_column(table_id, col_name, col_meta['type'], allowed_terms)
                if doc_rec:
                    if context_mode == "datastore":
                        logger.info(f"  [FOUND Datastore] Glossary recommendation found for '{col_name}'.")
                    elif context_mode == "direct":
                        logger.info(f"  [FOUND Direct] Glossary recommendation found for '{col_name}'.")
                    else:
                        logger.info(f"  [FOUND RAG] Glossary recommendation found for '{col_name}'.")
                        
                    term_display = doc_rec["Proposed Term"]
                    matched_term = next((t for t in all_terms if t['display_name'] == term_display), None)
                    term_id = matched_term['name'] if matched_term else term_display
                    
                    recommendations.append({
                        "Column": col_name,
                        "Suggested Term": term_display,
                        "Confidence": doc_rec["Confidence"],
                        "Rationale": doc_rec["Rationale"],
                        "Term ID": term_id
                    })
                    # Prioritize Document hits over Similarity
                    continue
                else:
                    if context_mode == "datastore":
                        logger.info(f"  [NOT FOUND Datastore] No glossary recommendation found for '{col_name}' in Datastore.")
                    elif context_mode == "direct":
                        logger.info(f"  [NOT FOUND Direct] No glossary recommendation found for '{col_name}' in document.")
                    else:
                        logger.info(f"  [NOT FOUND RAG] No glossary recommendation found for '{col_name}' in document.")

            # C. Similarity-Based Recommendations
            suggestions = self._similarity_engine.get_ranked_suggestions(col_meta, all_terms, col_embedding=col_emb)
            
            for sug in suggestions:
                term_id = sug['term_name']
                
                # Targeted check for existing link
                if self._check_link_exists(dataset_id, table_id, col_name, term_id):
                    continue

                # Also check legacy check (description based)
                if f"Business Glossary: {sug['display_name']}" in col_meta.get('description', ''):
                     continue

                recommendations.append({
                    "Column": col_name,
                    "Suggested Term": sug['display_name'],
                    "Confidence": sug['confidence'],
                    "Rationale": f"Lexical: {sug['signals']['lexical']}, Semantic: {sug['signals']['semantic']}",
                    "Term ID": term_id
                })
        
        logger.info(f"Generated {len(recommendations)} recommendations for {table_id} after deduplication.")
        return pd.DataFrame(recommendations)

    def _get_entry_name(self, dataset_id: str, table_id: str):
        entry_id = f"bigquery.googleapis.com/projects/{self.project_id}/datasets/{dataset_id}/tables/{table_id}"
        # Harvested entries are in the @bigquery group at the same location as the BQ dataset
        return f"projects/{self.project_id}/locations/{self.location}/entryGroups/@bigquery/entries/{entry_id}"

    def _resolve_term_entry_name(self, term_resource_name: str) -> Optional[str]:
        """Maps a Business Glossary term resource name to its Knowledge Catalog Entry name."""
        client = dataplex_v1.CatalogServiceClient(credentials=get_credentials(self.project_id))
        
        # We try deterministic patterns FIRST as they are faster and don't rely on eventual consistency of Search
        # and avoid 501/404 errors in certain regions/environments.

        # Pattern 1: Direct Construction with Project ID
        # Format: projects/{id}/locations/{loc}/entryGroups/@dataplex/entries/{resource}
        group_prefix = f"projects/{self.project_id}/locations/{self.location}/entryGroups/@dataplex/entries"
        candidate_id = f"{group_prefix}/{term_resource_name}"
        try:
            client.get_entry(name=candidate_id)
            return candidate_id
        except Exception:
            pass

        # Pattern 2: Direct Construction with Project Number (Harvested format)
        project_number = "1095607222622" # Hint for this specific demo environment
        term_res_num = term_resource_name.replace(self.project_id, project_number)
        candidate_num = f"{group_prefix}/{term_res_num}"
        try:
            client.get_entry(name=candidate_num)
            return candidate_num
        except Exception:
            pass

        # Pattern 3: Search fallback
        parent = f"projects/{self.project_id}/locations/{self.location}"
        query = f'resource:"{term_resource_name}"'
        try:
            request = dataplex_v1.SearchEntriesRequest(query=query)
            results = client.search_entries(request=request)
            for res in results:
                if "glossaries" in res.entry_source.resource:
                    return res.entry_name
        except Exception as e:
            # Downgrade to debug/info if construction works anyway, 
            # or if search is known to be flaky in this environment.
            logger.debug(f"Search fallback failed for glossary term resolution: {e}")

        logger.error(f"Could not resolve glossary term to Catalog Entry: {term_resource_name}")
        return None

    def apply_terms(self, dataset_id: str, table_id: str, updates: List[Dict[str, str]]):
        """
        Applies glossary terms to columns using native Dataplex EntryLinks.
        updates: List of {'column': str, 'term_id': str, 'term_display': str}
        """
        self._ensure_initialized()
        client = dataplex_v1.CatalogServiceClient(credentials=get_credentials(self.project_id))
        
        # 1. BigQuery update (Optional/Skipped as per previous preference)
        logger.info(f"Applying {len(updates)} glossary terms to {table_id} via native EntryLinks.")

        # EntryLinks for BigQuery entries MUST reside in the @bigquery EntryGroup
        parent_group = f"projects/{self.project_id}/locations/{self.location}/entryGroups/@bigquery"
        entry_name = self._get_entry_name(dataset_id, table_id)
        
        # Link Type for Glossary Definition
        link_type = "projects/dataplex-types/locations/global/entryLinkTypes/definition"

        for up in updates:
            column = up['column']
            term_resource_name = up['term_id']  # This is the Business Glossary resource name
            
            # Resolve to Catalog Entry Name
            term_entry_name = self._resolve_term_entry_name(term_resource_name)
            if not term_entry_name:
                logger.error(f"Skipping {column}: Could not resolve glossary term to Catalog Entry.")
                continue

            # Deterministic ID for idempotency: link_{table}_{column}
            # EntryLink IDs must be lowercase, alphanumeric/hyphens
            clean_column = column.replace("_", "-").lower()
            clean_table = table_id.replace("_", "-").lower()
            entry_link_id = f"link-{clean_table}-{clean_column}"
            
            try:
                # Create the EntryLink
                link = dataplex_v1.EntryLink()
                link.entry_link_type = link_type
                
                # Source: The Table Column
                source_ref = dataplex_v1.EntryLink.EntryReference()
                source_ref.name = entry_name
                source_ref.path = f"Schema.{column}"
                source_ref.type_ = dataplex_v1.EntryLink.EntryReference.Type.SOURCE
                
                # Target: The Glossary Term
                target_ref = dataplex_v1.EntryLink.EntryReference()
                target_ref.name = term_entry_name
                target_ref.type_ = dataplex_v1.EntryLink.EntryReference.Type.TARGET
                
                link.entry_references = [source_ref, target_ref]
                
                try:
                    # Create in @bigquery group
                    client.create_entry_link(parent=parent_group, entry_link_id=entry_link_id, entry_link=link)
                    logger.info(f"Created native link for {column} -> {up['term_display']} in @bigquery group")
                except Exception as e:
                    if "already exists" in str(e).lower():
                        logger.info(f"Link for {column} already exists, skipping.")
                    else:
                        raise e

            except Exception as e:
                logger.error(f"Failed to create EntryLink for {column}: {e}")
                # We continue with other updates even if one fails
                continue

    def scan_for_missing_glossary_terms(self, dataset_id: str) -> pd.DataFrame:
        """
        Scans all tables in a dataset for columns missing glossary terms using native EntryLinks.
        """
        self._ensure_initialized()
        client = dataplex_v1.CatalogServiceClient(credentials=get_credentials(self.project_id))
        
        parent = f"projects/{self.project_id}/locations/{self.location}"
        
        # NOTE: list_entry_links is currently restricted in this environment, 
        # so this scan relies on deterministic EntryLink ID checks.

        dataset_ref = self._bq_client.dataset(dataset_id)
        tables = self._bq_client.list_tables(dataset_ref)
        
        gaps = []
        for table_item in tables:
            table_id = table_item.table_id
            full_table = self._bq_client.get_table(table_item.reference)
            entry_name = self._get_entry_name(dataset_id, table_id)
            
            for field in full_table.schema:
                # 1. Check for native EntryLink (Deterministic CID)
                if self._is_column_linked(dataset_id, table_id, field.name):
                    continue

                # 2. Check legacy BQ description for backward compatibility
                desc = field.description or ""
                if "Business Glossary:" not in desc:
                    gaps.append({
                        "Table": table_id,
                        "Column": field.name,
                        "Type": field.field_type
                    })
        
        return pd.DataFrame(gaps)



