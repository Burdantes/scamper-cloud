#!/usr/bin/env python3

# requires pip install google-cloud-storage
from google.cloud import storage
from sys import argv
import os
from pathlib import Path

CREDENTIALS = "./nsf-2148275-66720-16168cbcf1c7.json"


def credential_path():
    for variable in ("SCAMPER_GCS_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS"):
        value = os.environ.get(variable)
        if value:
            return value

    script_dir = Path(__file__).resolve().parent
    json_keys = sorted(script_dir.glob("*.json"))
    if len(json_keys) == 1:
        return str(json_keys[0])
    return CREDENTIALS if Path(CREDENTIALS).is_file() else None


def storage_client():
    credentials = credential_path()
    if credentials:
        return storage.Client.from_service_account_json(credentials)
    return storage.Client()


def send_to_cloud_storage(file_name, bucket_name, object_name=None):
    attempt = 0
    blob = None
    success = False
    max_attempts = int(os.environ.get("SCAMPER_UPLOAD_MAX_ATTEMPTS", "5"))
    while not success and attempt < max_attempts:
        try:
            attempt += 1
            storage_client_instance = storage_client()
            bucket = storage_client_instance.get_bucket(bucket_name)
            blob = bucket.blob(object_name or Path(file_name).name)
            print("Uploading results to Cloud Storage (try #{}): {}".format(attempt, blob))
            blob.upload_from_filename(file_name)
            print('Successfully uploaded ({} attempts) {}.'.format(attempt, blob))
            success = True
        except Exception as err:
            print("Attempt {} failed to upload {} due to {}:{}".format(
                attempt, blob, Exception, err))
    if not success:
        raise RuntimeError(
            f"failed to upload {file_name} after {max_attempts} attempts"
        )


if __name__ == "__main__":
    os.chdir(os.path.dirname(argv[0]))
    send_to_cloud_storage(argv[1], argv[2], argv[3] if len(argv) > 3 else None)
