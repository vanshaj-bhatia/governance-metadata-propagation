import sys
import os
import logging
import pandas as pd
from typing import List, Dict, Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.cloud import bigquery
from google import genai
from google.genai import types
from document_rag_engine import DocumentRAGEngine
from context import get_credentials

logger = logging.getLogger(__name__)

class DocDescriptionPlugin(BasePlugin):
    def __init__(self, project_id: str, location: str = "europe-west1"):
        super().__init__(name="doc_description_plugin")
        self.project_id = project_id
        self.location = location
        self._bq_client = None
        self._rag_engine = None
        self._client = None

    def _ensure_initialized(self):
        creds = get_credentials(self.project_id)
        if not self._bq_client:
            self._bq_client = bigquery.Client(project=self.project_id, credentials=creds)
        if not self._rag_engine:
            self._rag_engine = DocumentRAGEngine(self.project_id, location=self.location, credentials=creds)
        if not self._client:
             self._client = genai.Client(
                    project=self.project_id,
                    location=self.location,
                    credentials=creds,
                    vertexai=True
                )

    def load_document(self, doc_path: Optional[List[str]], mode: str = "rag", datastore_id: Optional[str] = None):
        """Loads the document(s) or sets up the DataStore."""
        self._ensure_initialized()
        self.mode = mode
        self.datastore_id = datastore_id
        
        if not doc_path:
            return
            
        if self.mode == "rag":
            for path in doc_path:
                self._rag_engine.load_document(path)
        elif self.mode == "direct":
            self.full_text = ""
            import os
            for path in doc_path:
                ext = os.path.splitext(path)[1].lower()
                if ext in [".txt", ".md"]:
                    with open(path, 'r', encoding='utf-8') as f:
                        self.full_text += f.read() + "\n"
                elif ext == ".pdf":
                    # Use RAGEngine to extract text via Gemini
                    self.full_text += self._rag_engine._extract_text_via_gemini(path) + "\n"
        elif self.mode == "datastore" and datastore_id:
            # Fail Fast: Verify DataStore exists
            logger.info(f"Verifying DataStore '{datastore_id}'...")
            if not self._verify_datastore_exists():
                raise ValueError(f"DataStore '{datastore_id}' not found or not accessible.")

    def recommend_description_for_column(self, table_id: str, col_name: str, col_type: str) -> Optional[Dict[str, Any]]:
        """Recommends a description for a single column based on selected mode."""
        self._ensure_initialized()
        
        logger.info(f"Analyzing column '{col_name}' in table '{table_id}' using mode '{self.mode}'...")
        
        query = f"Table: {table_id}, Column: {col_name} ({col_type})"
        context = ""
        top_score = 0.8 # Default fallback
        
        if self.mode == "rag":
            chunks = self._rag_engine.retrieve(query, top_k=5)
            if chunks:
                context = "\n---\n".join([c['text'] for c in chunks])
                top_score = chunks[0]['score'] # Use top similarity score
                
        elif self.mode == "direct":
            if hasattr(self, 'full_text') and self.full_text:
                context = self.full_text
                
        elif self.mode == "datastore":
            if self.datastore_id:
                # Call Vertex AI Search API
                context = self._query_datastore(query)
                
        if not context:
            return None
            
        # Generate description via Gemini
        desc = self._generate_description_with_context(table_id, col_name, col_type, context)
        
        if desc:
            return {
                "Target Column": col_name,
                "Proposed Description": desc,
                "Confidence": round(top_score, 2), # Round to 2 decimals
                "Source": f"Document ({self.mode})",
                "Rationale": f"Generated using {self.mode} document processing"
            }
        return None

    def _query_datastore(self, query: str) -> str:
        """Queries the Vertex AI Search DataStore using REST API."""
        import requests
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        
        project_id = self.project_id
        location = "global" 
        datastore_id = self.datastore_id
        
        credentials, project = google.auth.default()
        authed_session = AuthorizedSession(credentials)
        
        # Handle full resource path vs short ID
        if datastore_id.startswith("projects/"):
            url = f"https://discoveryengine.googleapis.com/v1/{datastore_id}/servingConfigs/default_search:search"
        else:
            url = f"https://discoveryengine.googleapis.com/v1/projects/{project_id}/locations/{location}/collections/default_collection/dataStores/{datastore_id}/servingConfigs/default_search:search"
        
        payload = {
            "query": query,
            "pageSize": 10, # Increase page size to get more snippets across results
            "contentSearchSpec": {
                "snippetSpec": {
                    "maxSnippetCount": 3
                }
            }
        }
        
        try:
            response = authed_session.post(url, json=payload)
            if response.status_code == 200:
                data = response.json()
                results = data.get('results', [])
                snippets = []
                for result in results:
                    doc = result.get('document', {})
                    derived_data = doc.get('derivedStructData', {})
                    # Look for extractive segments first, fallback to snippets
                    segments = derived_data.get('extractive_segments', [])
                    if segments:
                        for s in segments:
                            snippets.append(s.get('content'))
                    else:
                        doc_snippets = derived_data.get('snippets', [])
                        for s in doc_snippets:
                            snippets.append(s.get('snippet'))
                        
                # Move to debug logs
                logger.debug(f"DataStore retrieved {len(snippets)} snippets for query: '{query}'")
                for i, s in enumerate(snippets):
                    logger.debug(f"  Snippet {i+1}: {s}")
                return "\n---\n".join(snippets)
            else:
                try:
                    err_data = response.json()
                    msg = err_data.get('error', {}).get('message', response.text)
                    logger.error(f"DataStore search failed: {msg}")
                except Exception:
                    logger.error(f"DataStore search failed with status {response.status_code}")
                return ""
        except Exception as e:
            logger.error(f"Exception querying DataStore: {e}")
            return ""

    def _verify_datastore_exists(self) -> bool:
        """Verifies that the DataStore exists by making a simple search call."""
        import requests
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        
        project_id = self.project_id
        location = "global"
        datastore_id = self.datastore_id
        
        credentials, project = google.auth.default()
        authed_session = AuthorizedSession(credentials)
        
        if datastore_id.startswith("projects/"):
            url = f"https://discoveryengine.googleapis.com/v1/{datastore_id}/servingConfigs/default_search:search"
        else:
            url = f"https://discoveryengine.googleapis.com/v1/projects/{project_id}/locations/{location}/collections/default_collection/dataStores/{datastore_id}/servingConfigs/default_search:search"
            
        payload = {
            "query": "test",
            "pageSize": 1
        }
        
        try:
            response = authed_session.post(url, json=payload)
            if response.status_code == 200:
                return True
            elif response.status_code == 404:
                return False
            else:
                logger.warning(f"DataStore verification returned status {response.status_code}")
                return True # Assume exists if not 404 for safety
        except Exception as e:
            logger.error(f"Exception verifying DataStore: {e}")
            return True # Assume exists to avoid false negatives on network issues

    def recommend_descriptions(self, dataset_id: str, table_id: str, doc_path: str) -> pd.DataFrame:
        """Recommends column descriptions based on unstructured document context (Batch)."""
        self._ensure_initialized()
        self.load_document(doc_path)
        
        table_ref = f"{self.project_id}.{dataset_id}.{table_id}"
        table = self._bq_client.get_table(table_ref)
        
        recommendations = []
        
        for field in table.schema:
            if field.description:
                continue
                
            rec = self.recommend_description_for_column(table_id, field.name, field.field_type)
            if rec:
                recommendations.append(rec)
                
        return pd.DataFrame(recommendations)

    def _generate_description_with_context(self, table_id: str, col_name: str, col_type: str, context: str) -> str:
        """Calls Gemini to generate a description based on context with strict grounding."""
        if not context:
            logger.warning(f"No context found for column {col_name}")
            return ""
            
        prompt = f"""
You are a Data Steward. Your task is to generate a short, professional description for a database column based on the provided reference documentation.

Table Name: {table_id}
Column Name: {col_name}
Column Type: {col_type}

Reference Documentation:
{context}

Instructions:
1. Generate a concise description (1-2 sentences) for the column based on the provided documentation.
2. The documentation might contain information about OTHER tables. You MUST ignore information that does not pertain to the table '{table_id}'.
3. If the documentation does not contain EXPLICIT information for this specific column in the context of table '{table_id}', you MUST reply with "NO_INFO". Do not infer, extrapolate, or assume meanings even if they seem logical.
4. Focus on the business meaning of the column.
"""
        try:
            response = self._client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0
                )
            )
            text = response.text.strip()
            if text == "NO_INFO":
                return ""
            return text
        except Exception as e:
            logger.error(f"Failed to generate description for {col_name}: {e}")
            return ""
