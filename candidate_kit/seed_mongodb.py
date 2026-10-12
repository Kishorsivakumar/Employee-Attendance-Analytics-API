
import json
import os
from pathlib import Path

from bson import json_util
from pymongo import MongoClient
from pymongo.errors import PyMongoError


# --------------------------------------------------
# Configuration
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

EMPLOYEES_FILE = BASE_DIR / "sample_data/employees.json"
ATTENDANCE_FILE = BASE_DIR / "sample_data/attendance_logs.json"

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")


def load_json_file(file_path: Path) -> list[dict]:
    """Load MongoDB Extended JSON, including BSON dates."""
    if not file_path.exists():
        raise FileNotFoundError(
            f"Seed file not found: {file_path}"
        )

    documents = json_util.loads(
        file_path.read_text(encoding="utf-8")
    )

    if not isinstance(documents, list):
        raise ValueError(
            f"Expected a JSON array in {file_path.name}"
        )

    return documents


def ensure_collection(db, collection_name: str) -> bool:
    """
    Create a missing collection.

    Return True if it was newly created, or if it
    already exists but is empty and needs seeding.
    """
    if collection_name not in db.list_collection_names():
        db.create_collection(collection_name)
        print(f"[CREATED] Collection: {collection_name}")
        return True

    collection = db[collection_name]

    if collection.count_documents({}) == 0:
        print(f"[EMPTY] Collection: {collection_name}")
        return True

    print(
        f"[SKIPPED] {collection_name} already contains "
        f"{collection.count_documents({})} documents."
    )
    return False


def seed_collection(db, collection_name: str, file_path: Path):
    """Seed a collection only when it is missing or empty."""
    should_seed = ensure_collection(db, collection_name)

    if not should_seed:
        return

    documents = load_json_file(file_path)

    if not documents:
        print(f"[WARNING] No documents in {file_path.name}")
        return

    # Remove supplied _id fields if present so MongoDB
    # can generate fresh ObjectIds.
    for document in documents:
        document.pop("_id", None)

    if collection_name == "employees":
        keys = [doc["emp_code"] for doc in documents]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate emp_code in employees.json")

    elif collection_name == "attendance_logs":
        keys = [
            (doc["emp_code"], doc["date"])
            for doc in documents
        ]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "Duplicate (emp_code, date) in attendance_logs.json"
            )

    result = db[collection_name].insert_many(documents)

    print(
        f"[SEEDED] {collection_name}: "
        f"{len(result.inserted_ids)} documents inserted."
    )


def main():
    client = None

    try:
        client = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=5000,
        )

        # Confirm MongoDB is reachable.
        client.admin.command("ping")
        print("[CONNECTED] MongoDB connection successful.")

        db = client[MONGO_DB]
        print(f"[DATABASE] {MONGO_DB}")

        seed_collection(
            db,
            "employees",
            EMPLOYEES_FILE,
        )

        seed_collection(
            db,
            "attendance_logs",
            ATTENDANCE_FILE,
        )

        print("\n--- Final collection counts ---")
        print("Employees:", db.employees.count_documents({}))
        print(
            "Attendance logs:",
            db.attendance_logs.count_documents({}),
        )
        print("\nSeeding process completed.")

    except (PyMongoError, OSError, ValueError, KeyError) as exc:
        print(f"[ERROR] Seeding failed: {exc}")
        raise

    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
