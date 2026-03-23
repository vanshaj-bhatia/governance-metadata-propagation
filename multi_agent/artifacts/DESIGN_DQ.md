# Technical Design: Data Quality (DQ) Propagation
**Status: Implemented (March 2026)**

## 1. Architectural Overview
The DQ Propagation module consists of three primary components:
1. **DQ Ingestor (Plugin)**: Interfaces with Google Cloud Dataplex to fetch the latest DQ and Profiling results.
2. **Propagation Engine**: Traverses the lineage graph and calculates derived scores for downstream nodes.
3. **Persistence Layer**: Manages a rolling history of DQ snapshots and steward overrides.

## 2. Component Detail

### 2.1 DQ Ingestor (`agent/plugins/dq_plugin.py`)
- **API Strategy**: Use `google.cloud.dataplex_v1.DataScanServiceClient`.
- **Method**: 
    - `list_data_scan_jobs(parent, view=FULL)` to find the most recent successful job for a table's scan.
    - Extract `data_quality_result` (for DQ scans) or `data_profile_result` (for Profiling scans).
- **Fallback Logic**:
    1. Check `data_quality_result.passed` and dimensions (Completeness, Uniqueness, etc.).
    2. If missing, check `data_profile_result.profile.fields` for null counts and distinct counts.
    3. If both missing, perform a lightweight BigQuery `COUNT(*)` vs `COUNT(column)` check for completeness.

### 2.2 Propagation Engine (`dataplex_integration/dq_propagation.py`)
- **Traversal**: Integrate with `LineageGraphTraverser`.
- **Scoring Metadata**: Fetch DQ scores for all upstream "leaf" tables.
- **Aggregation Logic**:
    - **`conservative_min`**: `min(upstream_scores)`. Ensures trust is only as high as the weakest link.
    - **`weighted_avg`**: `sum(score * weight) / sum(weights)`. Default weights based on column "importance" (e.g., identity columns = 1.0, auxiliary = 0.5).
- **Remediation Detection**:
    - Scans SQL transformations (via `SQLFetcher`) for keywords: `DISTINCT`, `UNIQUE`, `IS NOT NULL`, `COALESCE`, `IFNULL`.
    - If a "Quality Repair" pattern is detected, apply a **Bonus Factor** (e.g., +20% to the relevant dimension) to reflect the improvement.

### 2.3 DQ History Schema (`BigQuery`)
To track 5 snapshots, we will use a table `dq_propagation_history`:
- `table_fqn`: STRING
- `column_name`: STRING
- `snapshot_time`: TIMESTAMP
- `dq_score`: FLOAT64
- `dimensions`: JSON (Completeness, Validity, etc.)
- `source_type`: STRING (AUTO_DQ, PROFILING, DERIVED)

## 3. Propagation Algorithm
1. Identify target table or view (entity).
2. Resolve full upstream column lineage (multi-hop) across all entities.
3. For each source column, retrieve the latest 1.0-scaled DQ score (prioritizing Auto DQ).
4. Apply aggregation function (default: `conservative_min`).
5. Run transformation heuristics on intermediate SQL joins/selects to detect remediation.
6. Store derived score in the History table (locally in `dq_history.json` or BQ) and return to UI.

## 4. UI/CLI Integration
- **Badge logic**: 
    - `> 0.9`: Green (High Trust)
    - `0.7 - 0.9`: Yellow (Review Recommended)
    - `< 0.7`: Red (Caution)
- **Trend logic**: Compare current `dq_score` with the average of the previous 4 snapshots.
