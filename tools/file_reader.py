import pandas as pd
from interaction_log import log_action


def detect_columns(df: pd.DataFrame) -> dict:
    """
    Fuzzy matches column names to known field types.
    Now detects: name, business_name, email,
    and also extra context columns:
    location, category, subcategory, size, contact_info
    """
    columns       = list(df.columns)
    columns_lower = [c.lower().strip() for c in columns]
    col_map       = {c.lower().strip(): c for c in columns}
    result        = {}

    # NAME
    name_hints = [
        "name", "full name", "contact name", "first name",
        "person", "contact", "lead name", "prospect",
        "owner", "owner(s)", "founders", "founder"
    ]
    for hint in name_hints:
        if hint in columns_lower:
            result["name"] = col_map[hint]
            break
    if "name" not in result:
        for col in columns_lower:
            if "name" in col and "business" not in col:
                result["name"] = col_map[col]
                break

    # BUSINESS
    business_hints = [
        "business_name", "business name", "company",
        "company name", "organisation", "organization",
        "org", "firm", "brand", "account",
        "business", "startup", "agency", "venture",
        "store", "shop"
    ]
    for hint in business_hints:
        if hint in columns_lower:
            result["business_name"] = col_map[hint]
            break
    if "business_name" not in result:
        for col in columns_lower:
            if any(w in col for w in [
                "business", "company", "org",
                "firm", "brand"
            ]):
                result["business_name"] = col_map[col]
                break

    # EMAIL
    email_hints = [
        "email", "email address", "e-mail", "e mail",
        "mail", "contact email", "work email",
        "business email"
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

    # CONTACT INFO (website / social / mixed)
    contact_hints = [
        "contact info", "contact information",
        "contact details", "contact", "website",
        "web", "url", "link", "social"
    ]
    for hint in contact_hints:
        if hint in columns_lower:
            # Only use as contact_info if not already email
            col = col_map[hint]
            if col != result.get("email"):
                result["contact_info"] = col
            break

    # LOCATION
    location_hints = [
        "city", "city / location", "location",
        "city/location", "town", "region",
        "area", "address"
    ]
    for hint in location_hints:
        if hint in columns_lower:
            result["location"] = col_map[hint]
            break

    # COUNTRY
    country_hints = ["country", "nation", "country/region"]
    for hint in country_hints:
        if hint in columns_lower:
            result["country"] = col_map[hint]
            break

    # CONTINENT
    if "continent" in columns_lower:
        result["continent"] = col_map["continent"]

    # CATEGORY
    category_hints = [
        "category", "type", "industry",
        "sector", "niche", "category (food/non-food)"
    ]
    for hint in category_hints:
        if hint in columns_lower:
            result["category"] = col_map[hint]
            break

    # SUBCATEGORY
    sub_hints = [
        "sub-category", "subcategory", "sub category",
        "sub_category", "specialty", "product type"
    ]
    for hint in sub_hints:
        if hint in columns_lower:
            result["subcategory"] = col_map[hint]
            break

    # SIZE SIGNAL
    size_hints = [
        "size signal", "size", "company size",
        "team size", "signal"
    ]
    for hint in size_hints:
        if hint in columns_lower:
            result["size"] = col_map[hint]
            break

    return result


def ask_groq_to_map_columns(
    columns: list,
    user_id: str
) -> dict:
    """
    Falls back to Groq if fuzzy matching cannot
    identify the key columns.
    """
    prompt = f"""
I have a spreadsheet with these column names:
{columns}

Identify which column represents:
1. The person's name or owner name
2. The business or company name
3. The email address (if any)
4. A website URL or contact info field (if any)
5. A location or city field (if any)

Reply in this exact format only:
name_column: <exact column name or unknown>
business_column: <exact column name or unknown>
email_column: <exact column name or unknown>
contact_info_column: <exact column name or unknown>
location_column: <exact column name or unknown>
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
            elif "contact_info_column:" in line:
                val = line.split(":", 1)[1].strip()
                if val != "unknown":
                    mapping["contact_info"] = val
            elif "location_column:" in line:
                val = line.split(":", 1)[1].strip()
                if val != "unknown":
                    mapping["location"] = val

        return mapping

    except Exception as e:
        print(f"⚠️  Groq column mapping failed: {e}")
        return {}


def resolve_email(
    row_email:     str,
    contact_info:  str,
    business_name: str,
    location:      str = None
) -> str | None:
    """
    Tries to resolve a real email address for a contact.

    Priority order:
    1. If email column has a real email — use it
    2. If contact_info has a real email — use it
    3. If contact_info has a website URL — search for email
    4. Search by business name + location
    5. Give up — return None
    """
    import re
    from tools.email_finder import (
        is_url,
        is_social_media,
        find_email_from_website,
        find_email_from_business_name
    )

    email_pattern = r'[\w\.-]+@[\w\.-]+\.[a-zA-Z]{2,}'

    # 1 — Direct email in email column
    if row_email and "@" in str(row_email):
        match = re.search(email_pattern, str(row_email))
        if match:
            return match.group(0).lower()

    # 2 — Email hiding in contact_info column
    if contact_info and "@" in str(contact_info):
        match = re.search(email_pattern, str(contact_info))
        if match:
            return match.group(0).lower()

    # 3 — Website URL in contact_info — search for email
    if contact_info and is_url(str(contact_info)):
        email = find_email_from_website(
            business_name, str(contact_info)
        )
        if email:
            return email

    # 4 — Social media only or no useful info
    # Fall back to searching by business name
    if contact_info and is_social_media(str(contact_info)):
        print(
            f"📱 [FILE READER] {business_name} — "
            f"social only, searching by name..."
        )

    email = find_email_from_business_name(
        business_name, location
    )
    return email


def build_extra_context(row: pd.Series, mapping: dict) -> str:
    """
    Builds a rich context string from all the extra
    columns in the sheet — location, category, size etc.
    This gets passed to Groq alongside the research
    to make emails much more personalised.
    """
    parts = []

    location = str(
        row.get(mapping.get("location", ""), "")
    ).strip()
    country  = str(
        row.get(mapping.get("country", ""), "")
    ).strip()
    category = str(
        row.get(mapping.get("category", ""), "")
    ).strip()
    subcat   = str(
        row.get(mapping.get("subcategory", ""), "")
    ).strip()
    size     = str(
        row.get(mapping.get("size", ""), "")
    ).strip()

    if location and location != "nan":
        parts.append(f"Location: {location}")
    if country and country != "nan" and country != location:
        parts.append(f"Country: {country}")
    if category and category != "nan":
        parts.append(f"Category: {category}")
    if subcat and subcat != "nan":
        parts.append(f"Type: {subcat}")
    if size and size != "nan":
        parts.append(f"Size: {size}")

    return "\n".join(parts)


def build_contacts_from_df(
    df:      pd.DataFrame,
    user_id: str = "system"
) -> list[dict]:
    """
    Takes any DataFrame and returns a clean list
    of contact dicts — now with email resolution
    and rich context from all available columns.
    """
    # Try fuzzy matching first
    mapping = detect_columns(df)

    print(f"🗂️  [FILE READER] Column mapping: {mapping}")

    # Ask Groq for anything fuzzy matching missed
    missing_critical = [
        k for k in ["name", "business_name"]
        if k not in mapping
    ]

    if missing_critical:
        print(
            f"⚠️  [FILE READER] Could not detect: "
            f"{missing_critical} — asking Riley"
        )
        groq_mapping = ask_groq_to_map_columns(
            list(df.columns), user_id
        )
        for key in missing_critical:
            if key in groq_mapping:
                mapping[key] = groq_mapping[key]

    # Last resort positional fallback
    all_cols = list(df.columns)
    if "name" not in mapping and len(all_cols) >= 1:
        mapping["name"] = all_cols[0]
        print(f"⚠️  Using '{all_cols[0]}' as name")
    if "business_name" not in mapping and len(all_cols) >= 2:
        mapping["business_name"] = all_cols[1]
        print(f"⚠️  Using '{all_cols[1]}' as business_name")

    contacts        = []
    skipped_no_email = []

    for _, row in df.iterrows():
        name     = str(
            row.get(mapping.get("name", ""), "")
        ).strip()
        business = str(
            row.get(mapping.get("business_name", ""), "")
        ).strip()
        location = str(
            row.get(mapping.get("location", ""), "")
        ).strip()

        # Skip empty rows
        if not name or name == "nan":
            continue
        if not business or business == "nan":
            continue

        # Get raw email and contact info fields
        raw_email = str(
            row.get(mapping.get("email", ""), "")
        ).strip()
        contact_info = str(
            row.get(mapping.get("contact_info", ""), "")
        ).strip()

        # Clean up nan strings
        raw_email    = "" if raw_email == "nan" else raw_email
        contact_info = "" if contact_info == "nan" else contact_info
        location     = "" if location == "nan" else location

        print(
            f"▸  [FILE READER] Processing: "
            f"{name} @ {business}"
        )

        # Resolve email — tries multiple strategies
        email = resolve_email(
            row_email=raw_email,
            contact_info=contact_info,
            business_name=business,
            location=location
        )

        if not email:
            print(
                f"⚠️  [FILE READER] No email found for "
                f"{name} @ {business} — will skip"
            )
            skipped_no_email.append(
                f"{name} @ {business}"
            )
            continue

        # Build rich context from all extra columns
        extra_context = build_extra_context(row, mapping)

        contacts.append({
            "name":          name,
            "business_name": business,
            "email":         email,
            "extra_context": extra_context
        })

        print(
            f"✅ [FILE READER] Added: {name} @ {business} "
            f"→ {email}"
        )

    return contacts, skipped_no_email


def read_contact_list(
    file_path: str,
    user_id:   str = "system"
) -> tuple[list[dict], list[str]]:
    """
    Reads CSV or Excel — any format, any column names.
    Automatically:
    - Detects all columns including extra context
    - Finds emails even when missing (via web search)
    - Returns (contacts, skipped_list)
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

        df.columns = [str(c).strip() for c in df.columns]
        print(
            f"📋 [FILE READER] Columns: {list(df.columns)}"
        )

        contacts, skipped = build_contacts_from_df(
            df, user_id
        )

        if not contacts and not skipped:
            raise ValueError(
                "No valid contacts found in the file."
            )

        log_action(
            action_type="file_read",
            detail=(
                f"Read {len(contacts)} contacts, "
                f"skipped {len(skipped)} (no email found)"
            )
        )

        print(
            f"✅ [FILE READER] Done — "
            f"{len(contacts)} contacts, "
            f"{len(skipped)} skipped"
        )
        return contacts, skipped

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
) -> tuple[list[dict], list[str]]:
    """
    Parses a table pasted directly into Slack.
    Supports pipe-separated and tab-separated formats.
    Returns (contacts, skipped_list).
    """
    try:
        lines = [
            l.strip() for l in text.strip().split("\n")
            if l.strip()
        ]
        lines = [
            l for l in lines
            if not (
                set(l.replace("|", "").replace(" ", ""))
                <= set("-=")
            )
        ]

        if len(lines) < 2:
            return [], []

        if "|" in lines[0]:
            separator = "|"
        elif "\t" in lines[0]:
            separator = "\t"
        else:
            return [], []

        headers = [
            h.strip()
            for h in lines[0].split(separator)
            if h.strip()
        ]

        if len(headers) < 2:
            return [], []

        rows = []
        for line in lines[1:]:
            values = [v.strip() for v in line.split(separator)]
            while len(values) < len(headers):
                values.append("")
            values = values[:len(headers)]
            rows.append(dict(zip(headers, values)))

        if not rows:
            return [], []

        df = pd.DataFrame(rows)
        df.columns = [str(c).strip() for c in df.columns]

        print(
            f"📋 [PASTED TABLE] Columns: {list(df.columns)}"
        )

        contacts, skipped = build_contacts_from_df(
            df, user_id
        )

        if contacts:
            log_action(
                action_type="file_read",
                detail=(
                    f"Parsed {len(contacts)} contacts "
                    f"from pasted table"
                )
            )

        return contacts, skipped

    except Exception as e:
        print(f"⚠️  Could not parse pasted table: {e}")
        return [], []