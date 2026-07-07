import pandas as pd
from interaction_log import log_action


def read_contact_list(file_path: str) -> list[dict]:
    """
    Reads a CSV or Excel file and returns a clean list
    of contact dictionaries.

    Expected columns: name, business_name, email

    Returns:
        [
            {
                "name":          "Priya Sharma",
                "business_name": "GreenLeaf Organics",
                "email":         "priya@greenleaf.com"
            },
            ...
        ]
    """
    try:
        # Detect file type and read accordingly
        if file_path.endswith(".csv"):
            df = pd.read_csv(file_path)
        elif file_path.endswith((".xlsx", ".xls")):
            df = pd.read_excel(file_path)
        else:
            raise ValueError(
                f"Unsupported file type. "
                f"Please upload a .csv or .xlsx file."
            )

        # Clean up column names — strip spaces,
        # lowercase — so "Name " and "name" both work
        df.columns = df.columns.str.strip().str.lower()

        # Check required columns exist
        required = {"name", "business_name", "email"}
        missing  = required - set(df.columns)
        if missing:
            raise ValueError(
                f"Missing columns in your file: {missing}. "
                f"Required: name, business_name, email"
            )

        # Drop any rows where name or email is empty
        df = df.dropna(subset=["name", "email"])

        # Convert to list of clean dictionaries
        contacts = []
        for _, row in df.iterrows():
            contacts.append({
                "name":          str(row["name"]).strip(),
                "business_name": str(row["business_name"]).strip(),
                "email":         str(row["email"]).strip().lower()
            })

        # Log the action
        log_action(
            action_type="file_read",
            detail=f"Read {len(contacts)} contacts from file"
        )

        print(f"✅ File read — {len(contacts)} contacts loaded")
        return contacts

    except ValueError as e:
        # Known errors — bad format, missing columns
        log_action(
            action_type="error",
            detail=f"File read failed: {str(e)}"
        )
        raise

    except Exception as e:
        # Unknown errors — corrupt file etc
        log_action(
            action_type="error",
            detail=f"File read unexpected error: {str(e)}"
        )
        raise Exception(f"Could not read file: {str(e)}")