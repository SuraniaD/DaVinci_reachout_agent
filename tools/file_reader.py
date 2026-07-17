import pandas as pd
from interaction_log import log_action


def detect_columns(df: pd.DataFrame) -> dict:
    """
    Fuzzy matches column names to name, business_name, email.
    Handles variations like Full Name, Company, Email Address etc.
    """
    columns       = list(df.columns)
    columns_lower = [c.lower().strip() for c in columns]
    col_map       = {c.lower().strip(): c for c in columns}
    result        = {}

    # NAME
    name_hints = [
        "name", "full name", "contact name", "first name",
        "person", "contact", "lead name", "prospect", "owner"
    ]
    for hint in name_hints:
        if hint in columns_lower:
            result["name"] = col_map[hint]
            break
    if "name" not in result:
        for col in columns_lower:
            if "name" in col:
                result["name"] = col_map[col]
                break

    # BUSINESS
    business_hints = [
        "business_name", "business name", "company",
        "company name", "organisation", "organization",
        "org", "firm", "brand", "account", "employer",
        "business", "startup", "agency", "venture"
    ]
    for hint in business_hints:
        if hint in columns_lower:
            result["business_name"] = col_map[hint]
            break
    if "business_name" not in result:
        for col in columns_lower:
            if any(w in col for w in [
                "company", "business", "org", "firm", "brand"
            ]):
                result["business_name"] = col_map[col]
                break

    # EMAIL
    email_hints = [
        "email", "email address", "e-mail",
        "e mail", "mail", "contact email",
        "work email", "business email"
    ]
    for hint in email_hints:
        if hint in columns_lower:
            result["email"] = col_map[hint]
            break
    if "email" not in result:
        for col in columns_lower:
            if "mail" in col or "email" in col:
                result["email"] = col_map[col]
                break

    return result


def ask_groq_to_map_columns(columns: list, user_id: str) -> dict:
    """
    Falls back to Groq if fuzzy matching cannot identify columns.
    """
    prompt = f"""
I have a spreadsheet with these column names:
{columns}

Identify which column represents:
1. The person's name
2. The business or company name
3. The email address

Reply in this exact format only:
name_column: <exact column name>
business_column: <exact column name>
email_column: <exact column name>

If you cannot identify a column write: unknown
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


def build_contacts_from_df(
    df:      pd.DataFrame,
    user_id: str = "system"
) -> list[dict]:
    """
    Shared helper — takes any DataFrame and returns
    a clean list of contact dicts.
    Used by both read_contact_list and parse_pasted_table.
    """
    # Try fuzzy matching first
    mapping = detect_columns(df)

    # Ask Groq for anything fuzzy matching missed
    missing = [
        k for k in ["name", "business_name", "email"]
        if k not in mapping
    ]

    if missing:
        print(f"⚠️ Could not detect: {missing} — asking Riley")
        groq_mapping = ask_groq_to_map_columns(
            list(df.columns), user_id
        )
        for key in missing:
            if key in groq_mapping:
                mapping[key] = groq_mapping[key]

    # Last resort — use column position
    all_cols = list(df.columns)
    if "name" not in mapping and len(all_cols) >= 1:
        mapping["name"] = all_cols[0]
        print(f"⚠️ Using '{all_cols[0]}' as name")
    if "business_name" not in mapping and len(all_cols) >= 2:
        mapping["business_name"] = all_cols[1]
        print(f"⚠️ Using '{all_cols[1]}' as business_name")
    if "email" not in mapping and len(all_cols) >= 3:
        mapping["email"] = all_cols[2]
        print(f"⚠️ Using '{all_cols[2]}' as email")

    print(f"✅ Column mapping: {mapping}")

    # Build clean contact list
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

        if not name or not email:
            continue
        if "@" not in email:
            continue

        contacts.append({
            "name":          name,
            "business_name": business or "Unknown Business",
            "email":         email
        })

    return contacts


def read_contact_list(
    file_path: str,
    user_id:   str = "system"
) -> list[dict]:
    """
    Reads CSV or Excel file uploaded to Slack.
    Automatically detects columns regardless of header names.
    Supports: .csv, .xlsx, .xls

    Returns clean list of contact dicts:
    [{ name, business_name, email }, ...]
    """
    try:
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

        # Normalise column names
        df.columns = [
            str(c).strip() for c in df.columns
        ]

        print(f"📋 File columns found: {list(df.columns)}")

        contacts = build_contacts_from_df(df, user_id)

        if not contacts:
            raise ValueError(
                "No valid contacts found. "
                "Make sure the file has name and email columns."
            )

        log_action(
            action_type="file_read",
            detail=(
                f"Read {len(contacts)} contacts from file"
            )
        )

        print(f"✅ {len(contacts)} contacts loaded")
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
            detail=f"File read error: {str(e)}"
        )
        raise Exception(f"Could not read file: {str(e)}")


def parse_pasted_table(
    text:    str,
    user_id: str = "system"
) -> list[dict]:
    """
    Parses a table pasted directly into Slack as text.

    Supports two formats:

    Pipe-separated (copied from Notion, spreadsheet etc):
      Name         | Business      | Email
      Sarah Greene | Plant Baked   | sarah@plantbaked.com

    Tab-separated (copied from Excel, Google Sheets):
      Name         Business        Email
      Sarah Greene Plant Baked     sarah@plantbaked.com

    Returns same format as read_contact_list.
    Returns empty list if text doesn't look like a table.
    """
    try:
        # Split into non-empty lines
        lines = [
            l.strip() for l in text.strip().split("\n")
            if l.strip()
        ]

        # Filter out separator lines (---|---|--- style)
        lines = [
            l for l in lines
            if not (set(l.replace("|", "").replace(" ", "")) <= set("-="))
        ]

        if len(lines) < 2:
            # Need at least a header and one data row
            return []

        # Detect separator
        if "|" in lines[0]:
            separator = "|"
        elif "\t" in lines[0]:
            separator = "\t"
        else:
            # No clear separator — not a table
            return []

        # Parse header row
        headers = [
            h.strip()
            for h in lines[0].split(separator)
            if h.strip()
        ]

        if len(headers) < 2:
            return []

        # Parse data rows
        rows = []
        for line in lines[1:]:
            values = [
                v.strip()
                for v in line.split(separator)
            ]
            # Pad short rows, trim long rows
            while len(values) < len(headers):
                values.append("")
            values = values[:len(headers)]

            row_dict = dict(zip(headers, values))
            rows.append(row_dict)

        if not rows:
            return []

        # Build DataFrame so we can reuse detect_columns
        df = pd.DataFrame(rows)
        df.columns = [
            str(c).lower().strip() for c in df.columns
        ]

        print(
            f"📋 Pasted table columns: {list(df.columns)}"
        )

        contacts = build_contacts_from_df(df, user_id)

        if contacts:
            log_action(
                action_type="file_read",
                detail=(
                    f"Read {len(contacts)} contacts "
                    f"from pasted table"
                )
            )
            print(
                f"✅ {len(contacts)} contacts "
                f"parsed from pasted table"
            )

        return contacts

    except Exception as e:
        print(f"⚠️ Could not parse pasted table: {e}")
        return []