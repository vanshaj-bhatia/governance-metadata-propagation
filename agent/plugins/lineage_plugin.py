import sys
import os
import logging
import pandas as pd
from typing import List, Dict, Any, Optional

# Add adk_integration and dataplex_integration to relative path for plugin execution
PLUGIN_DIR = os.path.dirname(__file__)
sys.path.append(os.path.abspath(os.path.join(PLUGIN_DIR, '../adk_integration')))
sys.path.append(os.path.abspath(os.path.join(PLUGIN_DIR, '../../dataplex_integration')))

from google.adk.plugins.base_plugin import BasePlugin
from google.oauth2.credentials import Credentials
from google.cloud import bigquery
from context import get_oauth_token, get_credentials
from lineage_propagation import LineageGraphTraverser, TransformationEnricher, SQLFetcher
from insights_connector import DataInsightsClient as DescriptionPropagator

logger = logging.getLogger(__name__)

class LineagePlugin(BasePlugin):
    def __init__(self, project_id: str, location: str = "europe-west1", knowledge_json_path: Optional[str] = None):
        super().__init__(name="lineage_plugin")
        self.project_id = project_id
        self.location = location
        self.knowledge_json_path = knowledge_json_path
        self._lineage_traverser = None
        self._description_propagator = None
        self._sql_fetcher = None

    def _get_credentials(self):
        return get_credentials(self.project_id)

    def _get_bq_client(self):
        creds = self._get_credentials()
        return bigquery.Client(project=self.project_id, credentials=creds)

    def _ensure_initialized(self):
        creds = self._get_credentials()
        token = get_oauth_token()
        
        if not self._lineage_traverser:
            self._lineage_traverser = LineageGraphTraverser(self.project_id, self.location, token=token)
            if self.knowledge_json_path:
                self._lineage_traverser.load_knowledge_insights(self.knowledge_json_path)
            
        if not self._description_propagator:
            self._description_propagator = DescriptionPropagator(self.knowledge_json_path)
            
        if not self._sql_fetcher:
            self._sql_fetcher = SQLFetcher(self.project_id, self.location, credentials=creds)

    def scan_for_missing_descriptions(self, dataset_id: str) -> pd.DataFrame:
        """
        Scans a dataset for tables/columns missing descriptions.
        Returns a DataFrame.
        """
        self._ensure_initialized()
        client = self._get_bq_client()
        dataset_ref = f"{self.project_id}.{dataset_id}"
        
        tables = list(client.list_tables(dataset_ref))
        missing_data = []

        for table_item in tables:
            table_ref = f"{dataset_ref}.{table_item.table_id}"
            try:
                table = client.get_table(table_ref)
                for schema_field in table.schema:
                    if not schema_field.description:
                        missing_data.append({
                            "Table": table_item.table_id,
                            "Column": schema_field.name,
                            "Type": schema_field.field_type
                        })
            except Exception as e:
                logger.error(f"Error accessing {table_ref}: {e}")

        return pd.DataFrame(missing_data)

    def _find_description_recursive(self, target_fqn: str, column: str, depth: int = 0, max_depth: int = 5, accumulated_logic: List[str] = None) -> Optional[Dict[str, Any]]:
        """
        Recursively searches upstream for a description, accumulating SQL logic along the way.
        """
        if accumulated_logic is None:
            accumulated_logic = []
            
        if depth >= max_depth:
            return None
            
        # 1. Get immediate upstream
        upstream = self._lineage_traverser.get_column_lineage(target_fqn, [column], depth=depth)
        sources = upstream.get(column, [])
        
        if not sources:
            return None
        
        # 2. Extract SQL logic for the CURRENT target column to help pick the best source
        logic = None
        try:
            parts = target_fqn.replace("bigquery:", "").split('.')
            if len(parts) == 3:
                ds_id, tab_id = parts[1], parts[2]
                sql = self._sql_fetcher.get_transformation_sql(ds_id, tab_id)
                if sql:
                    logic = TransformationEnricher.extract_column_logic(sql, column)
                    if logic:
                        accumulated_logic.append(logic)
        except Exception as e:
            logger.debug(f"Failed to extract intermediate SQL logic for {target_fqn}: {e}")

        # 3. Select the best source (prioritize one mentioned in SQL logic)
        source = sources[0] # Default to best by confidence
        if logic:
            logic_lower = logic.lower()
            for s in sources:
                src_col = s['source_column'].lower()
                # Check for exact word match in logic
                import re
                if re.search(rf"\b{src_col}\b", logic_lower):
                    source = s
                    source['confidence'] = max(source['confidence'], 0.7)
                    break

        # 3. Check if source has description
        src_entity = source['source_fqn'].replace("bigquery:", "")
        src_col = source['source_column']
        
        try:
            client = self._get_bq_client()
            src_table = client.get_table(src_entity)
            for f in src_table.schema:
                if f.name == src_col:
                    if f.description:
                        # Found it!
                        return {
                            "source_entity": source['source_entity'],
                            "source_column": src_col,
                            "description": f.description,
                            "confidence": source['confidence'],
                            "hop_depth": depth,
                            "accumulated_logic": accumulated_logic
                        }
                    else:
                        # No description here, keep going up
                        return self._find_description_recursive(source['source_fqn'], src_col, depth + 1, max_depth, accumulated_logic)
        except Exception as e:
            logger.warning(f"Failed to check desc for {src_entity}.{src_col}: {e}")
            
        return None

    def preview_propagation(self, dataset_id: str, target_table: str) -> pd.DataFrame:
        """
        Simulates description propagation for a specific table with multi-hop support and SQL parsing.
        """
        self._ensure_initialized()
        target_fqn = f"bigquery:{self.project_id}.{dataset_id}.{target_table}"
        client = self._get_bq_client()
        table_ref = f"{self.project_id}.{dataset_id}.{target_table}"
        table = client.get_table(table_ref)
        
        candidates = []
        logger.info(f"--- Propagation Preview for {target_table} ---")
        for field in table.schema:
            if field.description:
                logger.debug(f"Skipping column '{field.name}' - already has description.")
                continue
                
            # Recursive search for this column
            logger.info(f"Searching source for column '{field.name}'...")
            match = self._find_description_recursive(target_fqn, field.name)
            
            if match:
                logger.info(f"  [FOUND] Source: {match['source_entity']}.{match['source_column']} -> {match['description'][:40]}...")
                # Enrich the found description using accumulated logic
                enriched_desc = TransformationEnricher.enrich_description(
                    field.name, 
                    match['source_column'], 
                    match['description'],
                    sql_hints=match.get('accumulated_logic', [])
                )
                
                candidates.append({
                    "Target Column": field.name,
                    "Source": match['source_entity'],
                    "Source Column": match['source_column'],
                    "Confidence": match['confidence'],
                    "Proposed Description": enriched_desc,
                    "Type": f"Lineage (Hop {match['hop_depth']})" if match['hop_depth'] > 0 else "Lineage"
                })
            else:
                logger.info(f"  [NOT FOUND] No source description found for '{field.name}'.")

        if not candidates:
            logger.warning(f"No propagation candidates found for {target_table}. (Missing desc count: {len([f for f in table.schema if not f.description])})")
        return pd.DataFrame(candidates)

    def get_lineage_summary(self, dataset_id: str, table_id: str) -> str:
        """
        Provides a holistic summary of upstream and downstream lineage.
        """
        self._ensure_initialized()
        full_table_name = f"{self.project_id}.{dataset_id}.{table_id}"
        client = self._get_bq_client()
        table = client.get_table(full_table_name)
        columns = [f.name for f in table.schema]
        
        # Upstream Analysis
        upstream_map = self._lineage_traverser.get_column_lineage(
            f"bigquery:{full_table_name}", 
            columns
        )
        upstream_entities = set()
        for candidates in upstream_map.values():
            for c in candidates:
                upstream_entities.add(c['source_entity'])
        
        # Downstream Analysis
        downstream_map = self._lineage_traverser.get_downstream_lineage(
            f"bigquery:{full_table_name}", 
            columns
        )
        downstream_entities = set()
        for targets in downstream_map.values():
            for t in targets:
                downstream_entities.add(t['target_entity'])
        
        # Fetch schemas for upstream entities to check for descriptions
        entity_descriptions = {}
        for ent in upstream_entities:
            try:
                clean_ent = ent.replace("bigquery:", "")
                src_table = client.get_table(clean_ent)
                entity_descriptions[ent] = {f.name for f in src_table.schema if f.description}
            except Exception as e:
                logger.warning(f"Failed to fetch schema for {ent}: {e}")
                entity_descriptions[ent] = set()

        # Generate Summary Text
        summary = f"### Propagation Summary for `{table_id}`\n\n"
        
        if upstream_entities:
            summary += f"**Upstream Sources ({len(upstream_entities)}):**\n"
            for ent in sorted(upstream_entities):
                # Count columns that have this entity as their PRIMARY (best) source AND have descriptions
                cols = []
                for c, candidates in upstream_map.items():
                    if candidates and candidates[0]['source_entity'] == ent:
                        src_col = candidates[0]['source_column']
                        if src_col in entity_descriptions.get(ent, set()):
                            cols.append(c)
                if cols:
                    summary += f"- `{ent}` (contributes {len(cols)} columns)\n"
                else:
                    summary += f"- `{ent}` (contributes 0 columns - missing descriptions upstream)\n"
        else:
            summary += "*No upstream sources found via Data Lineage API.*\n"
            
        summary += "\n"
        
        if downstream_entities:
            summary += f"**Downstream Targets ({len(downstream_entities)}):**\n"
            for ent in sorted(downstream_entities):
                # Count how many columns from this table flow into the downstream entity
                flowing_cols = set()
                for c, targets in downstream_map.items():
                    if any(t['target_entity'] == ent for t in targets):
                        flowing_cols.add(c)
                summary += f"- `{ent}` (receives {len(flowing_cols)} columns)\n"
        else:
            summary += "*No downstream targets found via Data Lineage API.*\n"
            
        summary += f"\n**Propagation Potential:**\n"
        all_columns = [f.name for f in table.schema]
        missing_desc = [f.name for f in table.schema if not f.description]
        potential_inherit = len([c for c in missing_desc if c in upstream_map])
        
        logger.info(f"Summary for {table_id}: total={len(all_columns)}, missing={len(missing_desc)}, lineage_mapped={len(upstream_map)}, potential={potential_inherit}")
        
        if not missing_desc:
            summary += f"✅ **This table is already fully documented in BigQuery.**\n"
            if downstream_entities:
                summary += f"- Metadata from this table is ready to propagate to **{len(downstream_entities)}** downstream entities.\n"
        else:
            summary += f"- {potential_inherit} missing columns can be enriched from upstream.\n"
            if downstream_entities:
                summary += f"- Metadata from this table can propagate to {len(downstream_entities)} downstream entities.\n"
            
        return summary

    def apply_propagation(self, dataset_id: str, updates: List[Dict[str, str]]):
        """
        Applies updates. 
        updates: List of dicts with keys 'table', 'column', 'description'
        """
        self._ensure_initialized()
        client = self._get_bq_client()
        
        for update in updates:
            table_id = update['table']
            col_name = update['column']
            desc = update['description']
            
            table_ref = f"{self.project_id}.{dataset_id}.{table_id}"
            table = client.get_table(table_ref)
            
            new_schema = []
            for field in table.schema:
                if field.name == col_name:
                    new_field = field.to_api_repr()
                    new_field['description'] = desc
                    new_schema.append(bigquery.SchemaField.from_api_repr(new_field))
                else:
                    new_schema.append(field)
            
            table.schema = new_schema
            client.update_table(table, ["schema"])
            logger.info(f"Updated {table_id}.{col_name}")
