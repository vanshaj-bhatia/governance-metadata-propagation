# Governance Metadata Propagation Demo

This project demonstrates an agentic data governance solution using Google Cloud Dataplex. It showcases how to automate metadata management, propagate insights via lineage, and leverage Dataplex **Dataset Insights** capabilities. This is only a demonstration and is not part of official product, please review everything before using it for your environments and use-cases.

---

## 🌟 Key Features

*   **Estate Dashboard**: Scan BigQuery datasets to identify metadata gaps (missing descriptions).
*   **Recursive Description Propagation**: Automatically fetch descriptions from upstream sources, bridging multi-hop gaps.
*   **SQL-Based Logic Enrichment**: Extracts BigQuery SQL transformations to generate human-readable descriptions for computed columns.
*   **AI Business Glossary**: Maps technical columns to business terms using Vertex AI Semantic Similarity.
*   **Prioritized Lineage Propagation**: Automatically propagates glossary terms across tables based on lineage, with strict verification thresholds to ensure accuracy (especially for 1-1 mappings).
*   **Native Dataplex Integration**: Persists glossary mappings as native `EntryLinks` visible in the Dataplex Schema tab.
*   **Unified UI & CLI**: Manage governance tasks via a Gradio-based web app or a headless CLI.
*   **Policy Tag Propagation**: Recommends and applies BigQuery policy tags via lineage, with support for "straight pull" detection and an integrated **Access Summary** (Readers & Data Policies).
*   **Data Trust Center (DQ)**: Derived trust scores for views and tables based on upstream Dataplex DQ/Profiling results and multi-hop lineage.
*   **Remediation Detection**: Automatically detects SQL transformations (e.g. `COALESCE`, `DISTINCT`) that improve data quality and applies "Trust Bonuses".
*   **Trust History Persistence**: Tracks 0.0-1.0 trust scores over time in BigQuery and local history for trend analysis.
*   **CLL API Preview allowlisting required**: Please contact your Google Cloud account team to get access to CLL API

---

## 🛠 Setup & Installation

### Prerequisites
- Python 3.12+
- Google Cloud Project with billing enabled.
- APIs Enabled: `dataplex`, `bigquery`, `datacatalog`, `datalineage`, `aiplatform`.

### Installation
1.  **Clone & Navigate**:
    ```bash
    git clone <repo-url>
    cd governance-metadata-propagation
    ```
2.  **Environment Setup**:
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    ```
3.  **Authentication**:
    - **CLI/Dev**: Run `gcloud auth application-default login`.
    - **Gradio App**: Follow [OAUTH_SETUP_GUIDE.md](OAUTH_SETUP_GUIDE.md) to enable "Login with Google".

---

## 🚀 Usage Guide

### 1. Agentic Data Steward (UI)
The Gradio app provides a visual way to scan and apply metadata changes.
```bash
python3 ui/gradio_app.py
```
- **Dashboard**: Run "Scan Dataset" to see health metrics.
- **Description Propagation**: Enter a table name to preview and apply lineage-based descriptions.
- **Policy Tag Propagation**: Propagate sensitive data tags across the lineage chain with automated transformation assessment.
- **Trust Center (DQ)**: Analyze derived trust scores for a view or table, showing upstream quality sources and remediation bonuses.
- **Settings**: Toggle OAuth/ADC modes for specific user actions.

### 3. 🐳 Deployment (Docker & Cloud Run)
For production or headless environments, the app is container-ready.

**Local Docker**:
```bash
# 1. Build
docker build -t steward-app .

# 2. Run (with local GCP credentials)
docker run -p 7860:7860 \
  --env-file .env \
  -v ~/.config/gcloud:/root/.config/gcloud \
  -e GOOGLE_APPLICATION_CREDENTIALS=/root/.config/gcloud/application_default_credentials.json \
  steward-app
```

**Cloud Run Deployment**:
```bash
chmod +x deploy.sh
./deploy.sh
```
*Note: Ensure your Service URL is added to the Authorized Redirect URIs in your GCP OAuth Client credentials.*

### 4. Steward CLI (Headless)
The CLI is designed for automation and quick scans.
```bash
# Scan a dataset for missing descriptions
python3 steward_cli.py scan --dataset retail_syn_data

# Preview and apply description propagation to a table
python3 steward_cli.py apply --dataset retail_syn_data --table transactions

# Recommend glossary terms using Vertex AI Semantic Similarity
python3 steward_cli.py glossary-recommend --dataset retail_syn_data --table transactions

# Scan a dataset for existing policy tags
python3 steward_cli.py policy-scan --dataset retail_syn_data

# Preview and apply policy tag propagation to a table
python3 steward_cli.py policy-propagate --dataset retail_syn_data --table transactions --apply

# Analyze and propagate trust/DQ scores for a view or table
python3 steward_cli.py dq-propagate --dataset retail_syn_data --table customers

# NEW: End-to-end Dataplex AI Insight propagation (Trigger -> Extract -> Apply)
# This handles the full scan, wait, and metadata update in one go.
python3 steward_cli.py dataplex-propagate --dataset retail_syn_data --table transactions --apply
```

### 3. Data Integration Scripts
- **Generate Data**: `python3 data_generation/generate_data.py` (Creates tables + lineage).
- **Unified Insights**: `python3 dataplex_integration/insights_connector.py` (Triggers, waits and extracts documentation results).

---

## 🧩 Project Modules

| Module | Location | Description |
| :--- | :--- | :--- |
| **Glossary Plugin** | `agent/plugins/glossary_plugin.py` | Handles Business Glossary mapping using Vertex AI. |
| **Lineage Plugin** | `agent/plugins/lineage_plugin.py` | Orchestrates description propagation via Lineage API. |
| **Policy Tag Plugin** | `agent/plugins/policy_tag_plugin.py` | Recommends and applies Policy Tags based on lineage and SQL analysis. |
| **Similarity Engine** | `agent/plugins/similarity_engine.py` | AI logic for scoring lexical and semantic matches. |
| **DQ Plugin** | `agent/plugins/dq_plugin.py` | Interfaces with Dataplex DQ/Profiling result jobs with BQ fallbacks. |
| **DQ Propagation** | `dataplex_integration/dq_propagation.py` | Recursive DQ scoring and remediation detection logic. |
| **Traverser** | `dataplex_integration/lineage_propagation.py` | Low-level Graph API logic for traversing dependencies. |
| **Enricher** | `dataplex_integration/lineage_propagation.py` | Context-aware SQL transformation analyzer. |

---

## 💡 Workflow Example

1.  **Initialize**: Generate synthetic data and lineage relationships.
2.  **Enrich & Propagate**: Run the unified `dataplex-propagate` command to trigger AI scans and sync metadata.
4.  **Tag**: Use the **Glossary Plugin** to map technical columns to the Business Glossary for Dataplex UI visibility.
5.  **Secure**: Use the **Policy Tag Propagation** plugin to sync sensitive data tags and verify access summary (Readers/Masking Rules).
6.  **Verify**: Check the **BigQuery Console** (Schema -> Policy Tags) and **Dataplex Schema** (Business Terms).
