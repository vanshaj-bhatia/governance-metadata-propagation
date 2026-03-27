# PRD: Data Quality (DQ) Propagation Module
**Status: Implemented (March 2026)**

## 1. Overview
The DQ Propagation module enables the propagation of trust metrics from high-level source tables to downstream views and materialized tables within the business ecosystem. It leverages the existing Data Lineage and Dataplex APIs to ensure that consumers of data have real-time visibility into the quality and reliability of the data they use.

## 2. Core Functional Requirements

### 2.1 DQ Ingestion & Prioritization
- **Auto DQ Integration**: The system must prioritize results from Dataplex Auto-DQ scans (`DataScanServiceClient`).
- **Profiling Fallback**: If dedicated DQ results are missing, the system shall infer quality from Dataplex Profiling results (e.g., null counts, distinct counts).
- **Rule-Based Fallback**: Provide a mechanism for basic quality evaluations (completeness, regex, uniqueness) where no automated scans exist.

### 2.2 Propagation Logic
- **Lineage Traversal**: Use the Lineage API to map column lineage across all downstream entities (tables and views).
- **Multi-Hop Support**: Support propagation across multiple layers of entities (Table A -> View B -> Table C).

### 2.3 Aggregation Equations
For each target column, compute a derived DQ score using one of the following methods:
- **Conservative (min)**: `target_DQ = min(upstream_column_DQ)`.
- **Weighted Average**: Allow weights based on column importance or contribution to key KPIs.
- **Custom Rule**: Support user-defined formulas at the table/column level.

### 2.4 Remediation Detection (Heuristics)
- **Transformation Analysis**: Identify if a transformation "repairs" quality (e.g., `DISTINCT` for uniqueness, `COALESCE` for completeness).
- **Score Adjustment**: Apply a bonus/multiplier to the derived score if remediation steps are detected.

### 2.5 History & Trust Tracking
- **Retention**: Maintain a rolling window of **5 historical snapshots** of DQ scores.
- **Trend Detection**: Calculate the DQ trend (improving/degrading) to be displayed as a trend line in the UI.

### 2.6 Human Overrides
- **Steward Control**: Allow authorized data stewards to manually override derived scores for specific columns or entities.

## 3. Non-Functional Requirements
- **Performance**: Propagation calculation should complete within seconds for standard lineage depths (up to 5 hops).
- **Separability**: The agentic orchestration logic must remain in `multi_agent/` to keep it decoupled from the core application.

## 4. User Scenarios
- **Scenario A (Data Trust)**: A business analyst looking at a "Sales Dashboard" table sees a "Low Quality" badge because an upstream "Customer" table has high nullity in the `email` column.
- **Scenario B (Remediation)**: A data engineer applies a deduplication logic in a downstream view. The system detects this and reflects a higher "Uniqueness" score than the raw source table.
