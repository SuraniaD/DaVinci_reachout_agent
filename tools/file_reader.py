import pandas as pd
from agents.riley import chat_with_riley
from interaction_log import log_action


def detect_columns(df: pd.DataFrame) -> dict:
    """
    Looks at the actual column names in the uploaded file
    and figures out which column maps to name, business, and email.

    Uses fuzzy matching first — catches obvious variations like
    "Name", "Full Name", "Contact Name", "Company", "Org" etc.

    If fuzzy matching can't figure it out confidently,
    falls back to asking Groq to interpret the columns.

    Returns a dict like:
    {
        "name":          "Contact Name",
        "business_name": "Organisation",
        "email":         "Email Address"
    }
    """
    columns = list(df.columns)
    columns_lower = [c.lower().strip() for c in columns]

    # Build a map of original column → lowercased
    col_map = {c.lower().strip(): c for c in columns}

    result = {}

    # ── NAME COLUMN ──────────────────────────
    name_hints = [
        "name", "full name", "contact name", "first name",
        "person", "contact", "lead name", "prospect"
    ]
    for hint in name_hints:
        if hint in columns_lower:
            result["name"] = col_map[hint]
            break
    # Partial match fallback
    if "name" not in result:
        for col in columns_lower:
            if "name" in col:
                result["name"] = col_map[col]
                break

    # ── BUSINESS COLUMN ──────────────────────
    business_hints = [
        "business_name", "business name", "company",
        "company name", "organisation", "organization",
        "org", "firm", "brand", "account", "employer",
        "business", "startup", "agency"
    ]
    for hint in business_hints:
        if hint in columns_lower:
            result["business_name"] = col_map[hint]
            break
    # Partial match fallback
    if "business_name" not in result:
        for col in columns_lower:
            if any(w in col for w in [
                "company", "business", "org", "firm", "brand"
            ]):
                result["business_name"] = col_map[col]
                break

    # ── EMAIL COLUMN ─────────────────────────
    email_hints = [
        "email", "email address", "e-mail",
        "e mail", "mail", "contact email",
        "work email", "business email"
    ]
    for hint in email_hints:
        if hint in columns_lower:
            result["email"] = col_map[hint]
            break
    # Partial match fallback
    if "email" not in result:
        for col in columns_lower:
            if "mail" in col or "email" in col:
                result["email"] = col_map[col]
                break

    return result


def ask_riley_to_map_columns(columns: list, user_id: str) -> dict:
    """
    If fuzzy matching can't confidently identify columns,
    ask Groq to interpret them.

    For example if columns are:
    ["Prospect", "Venture", "Contact Info"]

    Groq figures out:
    {
        "name":          "Prospect",
        "business_name": "Venture",
        "email":         "Contact Info"
    }
    """
    prompt = f"""
I have a spreadsheet with these column names:
{columns}

I need to identify which column represents:
1. The person's name
2. The business or company name
3. The email address

Reply in this exact format and nothing else — no explanation:
name_column: <exact column name>
business_column: <exact column name>
email_column: <exact column name>

If you cannot identify a column confidently, write: unknown
"""
    try:
        from agents.riley import chat_with_riley
        response = chat_with_riley(user_id, prompt)

        mapping = {}
        for line in response.strip().split("\n"):
            if "name_column:" in line:
                val = line.split(":", 1)[1].strip()
                if val != "unknown":
                    mapping["name"] = val
            elif "business_column:" in line:
                val = line.split(":", 1)[1].strip()
                if val != "unknown":
                    mapping["business_name"] = val
            elif "email_column:" in line:
                val = line.split(":", 1)[1].strip()
                if val != "unknown":
                    mapping["email"] = val

        return mapping

    except Exception as e:
        print(f"⚠️ Groq column mapping failed: {e}")
        return {}


def read_contact_list(
    file_path: str,
    user_id: str = "system"
) -> list[dict]:
    """
    Reads a CSV or Excel file and returns a clean list
    of contact dictionaries.

    Automatically detects which columns map to
    name, business_name, and email — regardless of
    what the columns are actually called in the file.

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
        # ── READ THE FILE ────────────────────────
        if file_path.endswith(".csv"):
            df = pd.read_csv(file_path)
        elif file_path.endswith((".xlsx", ".xls")):
            df = pd.read_excel(file_path)
        else:
            raise ValueError(
                "Unsupported file type. "
                "Please upload a .csv or .xlsx file."
            )

        if df.empty:
            raise ValueError("The file is empty.")

        print(f"📋 File columns found: {list(df.columns)}")

        # ── DETECT COLUMN MAPPING ────────────────
        mapping = detect_columns(df)

        # If fuzzy matching missed any column
        # ask Groq to interpret the columns
        missing = []
        if "name" not in mapping:
            missing.append("name")
        if "business_name" not in mapping:
            missing.append("business_name")
        if "email" not in mapping:
            missing.append("email")

        if missing:
            print(
                f"⚠️ Could not auto-detect columns: {missing}. "
                f"Asking Riley to interpret..."
            )
            groq_mapping = ask_riley_to_map_columns(
                list(df.columns), user_id
            )
            # Merge — only fill in the gaps
            for key in missing:
                if key in groq_mapping:
                    mapping[key] = groq_mapping[key]

        print(f"✅ Column mapping resolved: {mapping}")

        # ── STILL MISSING AFTER GROQ? ────────────
        # Use whatever columns exist as best-effort fallback
        all_cols = list(df.columns)

        if "name" not in mapping and len(all_cols) >= 1:
            mapping["name"] = all_cols[0]
            print(f"⚠️ Falling back: using '{all_cols[0]}' as name")

        if "business_name" not in mapping and len(all_cols) >= 2:
            mapping["business_name"] = all_cols[1]
            print(
                f"⚠️ Falling back: "
                f"using '{all_cols[1]}' as business_name"
            )

        if "email" not in mapping and len(all_cols) >= 3:
            mapping["email"] = all_cols[2]
            print(f"⚠️ Falling back: using '{all_cols[2]}' as email")

        # ── BUILD CONTACT LIST ───────────────────
        contacts = []
        for _, row in df.iterrows():
            name = str(
                row.get(mapping.get("name", ""), "")
            ).strip()
            business = str(
                row.get(mapping.get("business_name", ""), "")
            ).strip()
            email = str(
                row.get(mapping.get("email", ""), "")
            ).strip().lower()

            # Skip rows with no name or no email
            if not name or not email:
                continue

            # Skip rows where email doesn't look like an email
            if "@" not in email:
                continue

            contacts.append({
                "name":          name,
                "business_name": business or "Unknown Business",
                "email":         email
            })

        if not contacts:
            raise ValueError(
                "No valid contacts found in the file. "
                "Make sure the file has at least "
                "name and email columns."
            )

        # ── LOG AND RETURN ───────────────────────
        log_action(
            action_type="file_read",
            detail=(
                f"Read {len(contacts)} contacts. "
                f"Column mapping: {mapping}"
            )
        )

        print(f"✅ {len(contacts)} contacts loaded successfully")
        return contacts

    except ValueError as e:
        log_action(
            action_type="error",
            detail=f"File read failed: {str(e)}"
        )
        raise

    except Exception as e:
        log_action(
            action_type="error",
            detail=f"File read unexpected error: {str(e)}"
        )
        raise Exception(f"Could not read file: {str(e)}")