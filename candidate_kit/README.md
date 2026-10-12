# Employee Attendance & Analytics API

A REST API built with **FastAPI** and **MongoDB** to manage employee records, attendance tracking, attendance regularization, and analytics.

## Features

- Employee creation and lookup
- Punch-in and punch-out with business-rule validation
- Attendance regularization with audit history
- Employee monthly attendance analytics
- Department summaries and attendance trends
- Late-arrival leaderboard
- MongoDB aggregation pipelines and indexes
- Administrative query-plan inspection endpoints

## Tech Stack

- Python 3.11+
- FastAPI
- MongoDB 6.0+
- PyMongo
- Uvicorn

## Project Structure

```text
app/
  __init__.py
  main.py
tests/
  test_main.py
requirements.txt
README.md
REVIEW.md
DECISIONS.md
DATA_MODEL.md
openapi.yaml
sample_data/
```

## Setup and Run

1. Install Python 3.11+ and MongoDB 6.0+.
2. Create and activate a virtual environment.
3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Configure the MongoDB connection using the environment variables expected by the application.
5. Start the API from the project root:

   ```bash
   python -m uvicorn app.main:app --reload
   ```

6. Open the API documentation at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

## Testing

Run the test suite from the project root:

```bash
pytest -q
```

Record actual test results before submission.

## Submission Notes

- `REVIEW.md` documents the starter-code review and identified defects.
- `DECISIONS.md` explains the key implementation decisions.
