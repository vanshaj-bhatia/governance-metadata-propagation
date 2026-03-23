import sys
import os
import unittest
import json
from unittest.mock import MagicMock, patch

# Add necessary paths
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../agent/plugins')))
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../dataplex_integration')))

from dq_plugin import DQPlugin
from dq_propagation import DQPropagationEngine

class TestDQPropagation(unittest.TestCase):
    def setUp(self):
        self.project_id = "test-project"
        self.location = "europe-west1"
        self.dq_plugin = DQPlugin(self.project_id, self.location)
        self.engine = DQPropagationEngine(self.project_id, self.location)
        
        # Cleanup history file if it exists
        if os.path.exists("dq_history.json"):
            os.remove("dq_history.json")

    @patch('dq_plugin.get_credentials')
    @patch('google.cloud.dataplex_v1.DataScanServiceClient')
    def test_fetch_dq_summary_auto_dq(self, mock_client_class, mock_creds):
        mock_client = mock_client_class.return_value
        
        # Mock List Job
        mock_job = MagicMock()
        mock_job.name = "job-1"
        mock_job.state = MagicMock(name="SUCCEEDED")
        # Ensure name comparison works
        mock_job.state.name = "SUCCEEDED"
        
        mock_client.list_data_scan_jobs.return_value = [mock_job]
        
        # Mock Get Job (with results)
        mock_job_full = MagicMock()
        mock_job_full.state = MagicMock(name="SUCCEEDED")
        mock_job_full.state.name = "SUCCEEDED"
        
        # Configure the pb response for MessageToDict
        # We'll just mock the return value of get_latest_dq_job instead of deep protobuf mocking
        with patch.object(DQPlugin, 'get_latest_dq_job') as mock_get_job:
            mock_get_job.return_value = {
                "dataQualityResult": {
                    "passed": True,
                    "dimensions": [
                        {"dimension": "COMPLETENESS", "passed": True},
                        {"dimension": "UNIQUENESS", "passed": False}
                    ]
                },
                "endTime": "2024-03-20T10:00:00Z"
            }
            
            summary = self.dq_plugin.fetch_dq_summary("ds", "tab")
            self.assertEqual(summary['source'], "AUTO_DQ")
            self.assertEqual(summary['score'], 1.0)
            self.assertEqual(summary['dimensions']['COMPLETENESS'], 1.0)

    def test_aggregation_conservative_min(self):
        scores = [0.9, 0.7, 1.0]
        result = self.engine.aggregate_scores(scores, method="conservative_min")
        self.assertEqual(result, 0.7)

    @patch('dq_propagation.SQLFetcher.get_transformation_sql')
    def test_detect_remediation_distinct(self, mock_sql):
        # Mock SQL with DISTINCT
        mock_sql.return_value = "CREATE TABLE t AS SELECT DISTINCT id, name FROM src"
        
        bonus = self.engine.detect_remediation("ds", "table", "id")
        self.assertEqual(bonus, 0.1)

    def test_history_rolling_window(self):
        fqn = "bigquery:p.d.t"
        col = "c1"
        
        for i in range(10):
            self.engine.update_history(fqn, col, 0.5 + (i * 0.01))
            
        with open("dq_history.json", 'r') as f:
            history = json.load(f)
            
        snapshots = history[f"{fqn}#{col}"]
        self.assertEqual(len(snapshots), 5)
        # Latest should be at the top
        self.assertEqual(snapshots[0]['score'], 0.59)

    def test_trend_calculation(self):
        fqn = "bigquery:p.d.t"
        col = "c1"
        
        # Improving trend
        self.engine.update_history(fqn, col, 0.7) # old
        self.engine.update_history(fqn, col, 0.9) # new
        
        trend = self.engine.get_trend(fqn, col)
        self.assertEqual(trend, "improving")

    def tearDown(self):
        if os.path.exists("dq_history.json"):
            os.remove("dq_history.json")

if __name__ == '__main__':
    unittest.main()
