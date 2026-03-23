import os
import sys
import subprocess
from dotenv import load_dotenv
from google.cloud import datacatalog_v1

# Load configurations from .env
load_dotenv()

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "europe-west1")

def get_project_number(project_id):
    """Attempts to get the project number using gcloud."""
    gcloud_path = "/Users/akankshapb/google-cloud-sdk/bin/gcloud"
    try:
        result = subprocess.run(
            [gcloud_path, "projects", "describe", project_id, "--format=value(projectNumber)"],
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except Exception:
        return "[PROJECT_NUMBER]"

def get_taxonomy_id(project_id, location, display_name="Governance Test Taxonomy"):
    """Lists taxonomies to find the ID for the given display name."""
    client = datacatalog_v1.PolicyTagManagerClient()
    parent = f"projects/{project_id}/locations/{location}"
    try:
        taxonomies = client.list_taxonomies(parent=parent)
        for taxonomy in taxonomies:
            if taxonomy.display_name == display_name:
                # The name is in format projects/.../locations/.../taxonomies/[ID]
                return taxonomy.name.split("/")[-1]
    except Exception as e:
        print(f"Warning: Could not list taxonomies: {e}")
    return "[TAXONOMY_ID]"

def main():
    if not PROJECT_ID:
        print("❌ Error: GOOGLE_CLOUD_PROJECT not found in .env file.")
        sys.exit(1)

    print(f"Resolving IDs for project: {PROJECT_ID}...")
    
    project_number = get_project_number(PROJECT_ID)
    taxonomy_id = get_taxonomy_id(PROJECT_ID, LOCATION)
    
    service_account = f"service-{project_number}@gcp-sa-dataplex.iam.gserviceaccount.com"
    
    print("\n" + "="*60)
    print("🔐 DATAPLEX PERMISSION SETUP")
    print("="*60)
    print("\nTo allow Dataplex to scan policy-tagged data (like raw_customers),")
    print("run the following command in your terminal:\n")
    
    command = (
        f"gcloud data-catalog taxonomies add-iam-policy-binding {taxonomy_id} \\\n"
        f"    --location={LOCATION} \\\n"
        f"    --member=\"serviceAccount:{service_account}\" \\\n"
        f"    --role=\"roles/datacatalog.categoryFineGrainedReader\" \\\n"
        f"    --project={PROJECT_ID}"
    )
    
    print(command)
    print("\n" + "="*60)

if __name__ == "__main__":
    main()
