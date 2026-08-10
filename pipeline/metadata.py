"""
Dataset metadata utilities.

Every processed CSV gets a sidecar JSON with provenance information:
source, retrieval timestamp, latest observation date, units, transformations.
"""

import json
from datetime import datetime, timezone
from pathlib import Path


def save_metadata(csv_path, *, source, source_table, frequency, unit,
                  latest_observation_date=None, next_release_date=None,
                  transformations=None, notes=None, df=None, date_column="date"):
    """
    Save a metadata JSON sidecar next to a CSV file.

    Parameters
    ----------
    csv_path : str or Path
        Path to the CSV file (metadata JSON will be saved alongside)
    source : str
        Data source name (e.g., "Statistics Canada", "OECD")
    source_table : str
        Source table/dataset ID (e.g., "18-10-0004-01")
    frequency : str
        Data frequency ("monthly", "quarterly", "annual")
    unit : str
        Unit of measurement (e.g., "persons", "index", "% of GDP")
    latest_observation_date : str, optional
        Latest date in the data. Auto-detected from df if not provided.
    next_release_date : str, optional
        Next scheduled release date ("YYYY-MM-DD") for this series, sourced (not
        hardcoded) from Statistics Canada's release calendar — see
        pipeline/release_schedule.py. Used by config.get_next_release() to show a
        "Next update: <date>" note beside a chart's source line. Omit (None) for
        series with no published schedule.
    transformations : list of str, optional
        List of transformations applied
    notes : str, optional
        Additional notes/caveats
    df : DataFrame, optional
        The DataFrame being saved — used to auto-detect latest_observation_date
    date_column : str
        Name of the date column in df (default: "date")
    """
    csv_path = Path(csv_path)
    meta_path = csv_path.with_suffix(".json")

    # Auto-detect latest observation date from DataFrame
    if latest_observation_date is None and df is not None and date_column in df.columns:
        latest = df[date_column].max()
        if hasattr(latest, "isoformat"):
            latest_observation_date = latest.isoformat()[:10]
        else:
            latest_observation_date = str(latest)

    metadata = {
        "dataset_id": csv_path.stem,
        "source": source,
        "source_table": source_table,
        "frequency": frequency,
        "unit": unit,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "latest_observation_date": latest_observation_date,
        "next_release_date": next_release_date,
        "transformations": transformations or [],
        "notes": notes,
        "row_count": len(df) if df is not None else None,
    }

    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)

    return meta_path


def load_metadata(csv_path):
    """Load metadata JSON sidecar for a CSV file."""
    meta_path = Path(csv_path).with_suffix(".json")
    if meta_path.exists():
        with open(meta_path) as f:
            return json.load(f)
    return None


class SchemaError(Exception):
    """Raised when a DataFrame doesn't match its expected schema."""
    pass


def validate_columns(df, required_columns, dataset_name="dataset"):
    """Validate that a DataFrame has all required columns.

    Raises SchemaError if any required column is missing — this is
    intentionally loud to catch upstream schema changes early.

    Parameters
    ----------
    df : DataFrame
    required_columns : list of str
    dataset_name : str
        Name used in error messages
    """
    missing = [c for c in required_columns if c not in df.columns]
    if missing:
        available = list(df.columns)
        raise SchemaError(
            f"{dataset_name}: missing required columns {missing}. "
            f"Available columns: {available}"
        )
