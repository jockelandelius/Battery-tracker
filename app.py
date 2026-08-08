import csv
import io
import json
import os
import re
import sqlite3
import subprocess
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", BASE_DIR / "data" / "battery_tracker.db"))
ID_PREFIX_PATTERN = re.compile(r"^[A-Z0-9_-]+$")


def get_app_version(base_dir=BASE_DIR):
    release_path = base_dir / ".release"
    if release_path.is_file():
        release = release_path.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-fA-F]{7,64}", release):
            return release[:12]
    try:
        result = subprocess.run(
            ["git", "-C", str(base_dir), "rev-parse", "--short=12", "HEAD"],
            capture_output=True, check=True, text=True, timeout=1,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "okänd"


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-before-exposing-the-app")
    app.config["DATABASE"] = DATABASE_PATH
    app.config["VERSION"] = get_app_version()

    @app.teardown_appcontext
    def close_db(exception=None):
        database = g.pop("db", None)
        if database is not None:
            database.close()

    @app.template_filter("swedish_number")
    def swedish_number(value, digits=0):
        if value is None:
            return "–"
        return f"{float(value):.{digits}f}".replace(".", ",")

    @app.template_filter("iso_date")
    def iso_date(value):
        return value or "–"

    @app.template_global()
    def health_class(health):
        if health is None:
            return "health-unknown"
        if health > 97:
            return "health-good"
        if health > 80:
            return "health-warning"
        return "health-danger"

    @app.context_processor
    def inject_app_version():
        return {"app_version": app.config["VERSION"]}

    @app.route("/")
    def index():
        database = get_db()
        type_count = database.execute("SELECT COUNT(*) FROM battery_types").fetchone()[0]
        battery_count = database.execute("SELECT COUNT(*) FROM batteries").fetchone()[0]
        active_count = database.execute("SELECT COUNT(*) FROM batteries WHERE status = 'Aktiv'").fetchone()[0]
        recent_charges = database.execute(
            """
            SELECT charges.*, batteries.identifier, battery_types.code AS type_code
            FROM charges
            JOIN batteries ON batteries.id = charges.battery_id
            JOIN battery_types ON battery_types.id = batteries.type_id
            ORDER BY charges.charged_on DESC, charges.created_at DESC
            LIMIT 8
            """
        ).fetchall()
        battery_types = database.execute(
            """
            SELECT battery_types.*, COUNT(batteries.id) AS battery_count
            FROM battery_types
            LEFT JOIN batteries ON batteries.type_id = battery_types.id
            GROUP BY battery_types.id
            ORDER BY battery_types.code
            """
        ).fetchall()
        return render_template(
            "index.html",
            type_count=type_count,
            battery_count=battery_count,
            active_count=active_count,
            recent_charges=recent_charges,
            battery_types=battery_types,
        )

    @app.route("/types")
    def type_list():
        battery_types = get_db().execute(
            """
            SELECT battery_types.*, COUNT(batteries.id) AS battery_count
            FROM battery_types
            LEFT JOIN batteries ON batteries.type_id = battery_types.id
            GROUP BY battery_types.id
            ORDER BY battery_types.code
            """
        ).fetchall()
        return render_template("type_list.html", battery_types=battery_types)

    @app.route("/import", methods=("GET", "POST"))
    def data_import():
        database = get_db()
        battery_types = database.execute("SELECT * FROM battery_types ORDER BY code").fetchall()
        if request.method == "GET":
            return render_template("import_start.html", battery_types=battery_types)

        action = request.form.get("action")
        import_kind = request.form.get("import_kind", "batteries")
        csv_text = request.form.get("csv_text", "")
        type_id = request.form.get("type_id", type=int)
        battery_type = get_type_or_404(type_id) if import_kind == "batteries" and type_id else None
        if import_kind not in {"batteries", "charges"}:
            flash("Välj en giltig importtyp.", "error")
            return render_template("import_start.html", battery_types=battery_types)
        if import_kind == "batteries" and battery_type is None:
            flash("Välj en batterityp för batteriimporten.", "error")
            return render_template("import_start.html", battery_types=battery_types)

        try:
            headers, rows, headerless = parse_csv_text(csv_text, import_kind)
        except ValueError as error:
            flash(str(error), "error")
            return render_template("import_start.html", battery_types=battery_types)

        import_options = get_import_options(import_kind, battery_type)
        if action == "map":
            return render_template(
                "import_mapping.html", battery_types=battery_types, battery_type=battery_type,
                import_kind=import_kind, csv_text=csv_text, headers=headers,
                suggested_mapping=suggest_import_mapping(headers, import_options),
                import_options=import_options, row_count=len(rows),
            )

        mapping = {index: request.form.get(f"mapping_{index}", "ignore") for index in range(len(headers))}
        import_rows, errors, warnings = build_import_rows(
            database, import_kind, battery_type, headers, rows, mapping,
            row_number_start=1 if headerless else 2,
        )
        if action == "preview":
            return render_template(
                "import_preview.html", battery_types=battery_types, battery_type=battery_type,
                import_kind=import_kind, csv_text=csv_text, headers=headers, mapping=mapping,
                import_rows=import_rows, errors=errors, warnings=warnings,
            )
        if action == "commit":
            confirmation_required = warnings and request.form.get("accept_warnings") != "yes"
            if errors or confirmation_required:
                for error in errors:
                    flash(error, "error")
                return render_template(
                    "import_preview.html", battery_types=battery_types, battery_type=battery_type,
                    import_kind=import_kind, csv_text=csv_text, headers=headers, mapping=mapping,
                    import_rows=import_rows, errors=errors, warnings=warnings,
                    confirmation_required=confirmation_required,
                )
            try:
                commit_import_rows(database, import_kind, import_rows)
            except sqlite3.Error:
                database.rollback()
                flash("Importen kunde inte sparas. Inga rader har importerats.", "error")
                return render_template(
                    "import_preview.html", battery_types=battery_types, battery_type=battery_type,
                    import_kind=import_kind, csv_text=csv_text, headers=headers, mapping=mapping,
                    import_rows=import_rows, errors=[], warnings=warnings,
                )
            skipped_count = sum(import_row["skip"] for import_row in import_rows)
            imported_count = len(import_rows) - skipped_count
            message = f"Importen är klar: {imported_count} rader har lagts till."
            if skipped_count:
                message += f" {skipped_count} rader med okända batteri-ID:n hoppades över."
            flash(message, "success")
            return redirect(url_for("battery_list" if import_kind == "batteries" else "index"))

        abort(400)

    @app.route("/types/new", methods=("GET", "POST"))
    def type_new():
        if request.method == "POST":
            code = request.form.get("code", "").strip().upper()
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            custom_fields = parse_custom_fields(request.form.getlist("field_label[]"))
            errors = []
            if not code or not ID_PREFIX_PATTERN.fullmatch(code):
                errors.append("Typkod måste innehålla A–Z, siffror, bindestreck eller understreck.")
            if not name:
                errors.append("Namn på batteritypen krävs.")
            database = get_db()
            if code and database.execute("SELECT 1 FROM battery_types WHERE code = ?", (code,)).fetchone():
                errors.append("Typkoden används redan.")
            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template("type_form.html", custom_fields=custom_fields)
            cursor = database.execute(
                "INSERT INTO battery_types (code, name, description) VALUES (?, ?, ?)",
                (code, name, description or None),
            )
            for position, field in enumerate(custom_fields):
                database.execute(
                    "INSERT INTO battery_type_fields (type_id, label, field_key, position) VALUES (?, ?, ?, ?)",
                    (cursor.lastrowid, field["label"], field["key"], position),
                )
            database.commit()
            flash(f"Batteritypen {code} är skapad.", "success")
            return redirect(url_for("type_detail", type_id=cursor.lastrowid))
        return render_template("type_form.html", custom_fields=[])

    @app.route("/types/<int:type_id>")
    def type_detail(type_id):
        database = get_db()
        battery_type = get_type_or_404(type_id)
        batteries = database.execute(
            """
            SELECT batteries.*, latest.capacity_mah AS latest_capacity, latest.charged_on AS last_charged,
                   CASE WHEN batteries.nominal_capacity_mah > 0 AND latest.capacity_mah IS NOT NULL
                   THEN (latest.capacity_mah * 100.0 / batteries.nominal_capacity_mah) END AS health
            FROM batteries
            LEFT JOIN (
                SELECT c.battery_id, c.capacity_mah, c.charged_on
                FROM charges c
                INNER JOIN (
                    SELECT battery_id, MAX(charged_on || '|' || printf('%010d', id)) AS max_key
                    FROM charges GROUP BY battery_id
                ) newest ON newest.battery_id = c.battery_id
                   AND c.charged_on || '|' || printf('%010d', c.id) = newest.max_key
            ) latest ON latest.battery_id = batteries.id
            WHERE batteries.type_id = ?
            ORDER BY batteries.identifier
            """,
            (type_id,),
        ).fetchall()
        fields = get_type_fields(type_id)
        return render_template("type_detail.html", battery_type=battery_type, batteries=batteries, fields=fields)

    @app.route("/types/<int:type_id>/edit", methods=("GET", "POST"))
    def type_edit(type_id):
        database = get_db()
        battery_type = get_type_or_404(type_id)
        battery_count = get_type_battery_count(type_id)
        fields = get_type_fields_with_usage(type_id)

        if request.method == "POST":
            code = request.form.get("code", "").strip().upper()
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            errors = validate_type(database, code, name, type_id)
            if battery_count and code != battery_type["code"]:
                errors.append("Typkoden kan inte ändras när typen har registrerade batterier.")

            fields_to_remove = []
            fields_to_rename = []
            retained_keys = set()
            for field in fields:
                submitted_label = request.form.get(f"field_label_{field['id']}")
                if submitted_label is None:
                    if field["has_data"]:
                        errors.append(f"Fältet {field['label']} kan inte tas bort eftersom det har data.")
                    else:
                        fields_to_remove.append(field)
                    continue
                submitted_label = submitted_label.strip()
                if not submitted_label:
                    errors.append("Ett befintligt fältnamn får inte vara tomt.")
                    continue
                retained_keys.add(field["field_key"])
                if field["has_data"] and submitted_label != field["label"]:
                    errors.append(f"Fältet {field['label']} kan inte ändras eftersom det har data.")
                elif submitted_label != field["label"]:
                    fields_to_rename.append((field["id"], submitted_label))

            new_fields = parse_custom_fields(request.form.getlist("new_field_label[]"), retained_keys)
            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template(
                    "type_edit.html", battery_type=battery_type, battery_count=battery_count,
                    fields=fields, new_fields=request.form.getlist("new_field_label[]"),
                )

            database.execute(
                "UPDATE battery_types SET code = ?, name = ?, description = ? WHERE id = ?",
                (code, name, description or None, type_id),
            )
            for field_id, label in fields_to_rename:
                database.execute("UPDATE battery_type_fields SET label = ? WHERE id = ?", (label, field_id))
            for field in fields_to_remove:
                remove_custom_field_values(type_id, field["field_key"])
                database.execute("DELETE FROM battery_type_fields WHERE id = ?", (field["id"],))
            next_position = max((field["position"] for field in fields), default=-1) + 1
            for field in new_fields:
                database.execute(
                    "INSERT INTO battery_type_fields (type_id, label, field_key, position) VALUES (?, ?, ?, ?)",
                    (type_id, field["label"], field["key"], next_position),
                )
                next_position += 1
            database.commit()
            flash(f"Batteritypen {code} är uppdaterad.", "success")
            return redirect(url_for("type_detail", type_id=type_id))

        return render_template("type_edit.html", battery_type=battery_type, battery_count=battery_count, fields=fields, new_fields=[])

    @app.post("/types/<int:type_id>/delete")
    def type_delete(type_id):
        database = get_db()
        battery_type = get_type_or_404(type_id)
        battery_count = get_type_battery_count(type_id)
        if battery_count:
            flash("Batteritypen kan inte tas bort eftersom den har registrerade batterier.", "error")
            return redirect(url_for("type_edit", type_id=type_id))
        database.execute("DELETE FROM battery_types WHERE id = ?", (type_id,))
        database.commit()
        flash(f"Batteritypen {battery_type['code']} är borttagen.", "success")
        return redirect(url_for("type_list"))

    @app.route("/batteries/new", methods=("GET", "POST"))
    def battery_new():
        database = get_db()
        battery_types = database.execute("SELECT * FROM battery_types ORDER BY code").fetchall()
        requested_type = request.values.get("type_id", type=int) or (battery_types[0]["id"] if battery_types else None)
        battery_type = get_type_or_404(requested_type) if requested_type else None
        fields = get_type_fields(requested_type) if requested_type else []
        next_sequence = get_next_battery_sequence(battery_type) if battery_type else "001"
        if request.method == "POST":
            if not battery_type:
                flash("Skapa först en batterityp.", "error")
                return redirect(url_for("type_new"))
            identifier, sequence_error = build_battery_identifier(battery_type, request.form.get("sequence", ""))
            brand = request.form.get("brand", "").strip()
            chemistry = request.form.get("chemistry", "").strip()
            voltage = parse_number(request.form.get("voltage", ""))
            country = request.form.get("country", "").strip()
            introduced_month = request.form.get("introduced_month", "").strip()
            nominal_capacity = parse_integer(request.form.get("nominal_capacity_mah", ""))
            status = request.form.get("status", "Aktiv")
            custom_values = {field["field_key"]: request.form.get(f"custom_{field['field_key']}", "").strip() for field in fields}
            errors = validate_battery(
                database, battery_type, identifier, brand, chemistry, voltage, introduced_month, nominal_capacity, status
            )
            if sequence_error:
                errors.insert(0, sequence_error)
            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template(
                    "battery_form.html", battery_types=battery_types, battery_type=battery_type,
                    fields=fields, next_sequence=next_sequence,
                )
            database.execute(
                """
                INSERT INTO batteries
                (type_id, identifier, brand, chemistry, voltage, country, introduced_month,
                 nominal_capacity_mah, status, custom_values)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    battery_type["id"], identifier, brand, chemistry, voltage, country or None,
                    introduced_month, nominal_capacity, status, json.dumps(custom_values, ensure_ascii=False),
                ),
            )
            database.commit()
            flash(f"Batteriet {identifier} är registrerat.", "success")
            return redirect(url_for("type_detail", type_id=battery_type["id"]))
        return render_template(
            "battery_form.html", battery_types=battery_types, battery_type=battery_type,
            fields=fields, next_sequence=next_sequence,
        )

    @app.route("/batteries")
    def battery_list():
        database = get_db()
        battery_types = database.execute("SELECT * FROM battery_types ORDER BY code").fetchall()
        selected_type_id = request.args.get("type_id", type=int)
        if selected_type_id:
            get_type_or_404(selected_type_id)
        batteries = database.execute(
            """
            SELECT batteries.*, battery_types.code AS type_code, battery_types.name AS type_name,
                   latest.capacity_mah AS latest_capacity, latest.charged_on AS last_charged,
                   CASE WHEN batteries.nominal_capacity_mah > 0 AND latest.capacity_mah IS NOT NULL
                   THEN (latest.capacity_mah * 100.0 / batteries.nominal_capacity_mah) END AS health
            FROM batteries
            JOIN battery_types ON battery_types.id = batteries.type_id
            LEFT JOIN (
                SELECT c.battery_id, c.capacity_mah, c.charged_on
                FROM charges c
                INNER JOIN (
                    SELECT battery_id, MAX(charged_on || '|' || printf('%010d', id)) AS max_key
                    FROM charges GROUP BY battery_id
                ) newest ON newest.battery_id = c.battery_id
                   AND c.charged_on || '|' || printf('%010d', c.id) = newest.max_key
            ) latest ON latest.battery_id = batteries.id
            WHERE (? IS NULL OR batteries.type_id = ?)
            ORDER BY batteries.identifier
            """,
            (selected_type_id, selected_type_id),
        ).fetchall()
        return render_template(
            "battery_list.html", batteries=batteries, battery_types=battery_types,
            selected_type_id=selected_type_id,
        )

    @app.route("/batteries/<int:battery_id>")
    def battery_detail(battery_id):
        database = get_db()
        battery = get_battery_or_404(battery_id)
        charges = database.execute(
            "SELECT * FROM charges WHERE battery_id = ? ORDER BY charged_on DESC, id DESC",
            (battery_id,),
        ).fetchall()
        fields = get_type_fields(battery["type_id"])
        custom_values = json.loads(battery["custom_values"] or "{}")
        latest = charges[0] if charges else None
        health = (latest["capacity_mah"] * 100 / battery["nominal_capacity_mah"]) if latest and battery["nominal_capacity_mah"] else None
        chart_points = [
            {"date": charge["charged_on"], "capacity": charge["capacity_mah"]}
            for charge in reversed(charges)
        ]
        return render_template(
            "battery_detail.html", battery=battery, charges=charges, fields=fields,
            custom_values=custom_values, latest=latest, health=health, chart_points=chart_points,
        )

    @app.route("/batteries/<int:battery_id>/edit", methods=("GET", "POST"))
    def battery_edit(battery_id):
        database = get_db()
        battery = get_battery_or_404(battery_id)
        fields = get_type_fields(battery["type_id"])
        custom_values = json.loads(battery["custom_values"] or "{}")
        if request.method == "POST":
            brand = request.form.get("brand", "").strip()
            chemistry = request.form.get("chemistry", "").strip()
            voltage = parse_number(request.form.get("voltage", ""))
            country = request.form.get("country", "").strip()
            introduced_month = request.form.get("introduced_month", "").strip()
            nominal_capacity = parse_integer(request.form.get("nominal_capacity_mah", ""))
            status = request.form.get("status", "Aktiv")
            errors = validate_battery(
                database, None, None, brand, chemistry, voltage, introduced_month, nominal_capacity, status
            )
            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template(
                    "battery_edit.html", battery=battery, fields=fields, custom_values=custom_values,
                )
            updated_custom_values = dict(custom_values)
            for field in fields:
                updated_custom_values[field["field_key"]] = request.form.get(
                    f"custom_{field['field_key']}", ""
                ).strip()
            database.execute(
                """
                UPDATE batteries
                SET brand = ?, chemistry = ?, voltage = ?, country = ?, introduced_month = ?,
                    nominal_capacity_mah = ?, status = ?, custom_values = ?
                WHERE id = ?
                """,
                (
                    brand, chemistry, voltage, country or None, introduced_month, nominal_capacity,
                    status, json.dumps(updated_custom_values, ensure_ascii=False), battery_id,
                ),
            )
            database.commit()
            flash(f"Batteriet {battery['identifier']} är uppdaterat.", "success")
            return redirect(url_for("battery_detail", battery_id=battery_id))
        return render_template("battery_edit.html", battery=battery, fields=fields, custom_values=custom_values)

    @app.route("/charges/new", methods=("GET", "POST"))
    def charge_new():
        database = get_db()
        batteries = database.execute(
            "SELECT batteries.id, batteries.identifier, battery_types.code FROM batteries JOIN battery_types ON battery_types.id = batteries.type_id ORDER BY batteries.identifier"
        ).fetchall()
        selected_battery_id = request.values.get("battery_id", type=int)
        if request.method == "POST":
            battery_id = request.form.get("battery_id", type=int)
            charged_on = request.form.get("charged_on", "")
            capacity = parse_integer(request.form.get("capacity_mah", ""))
            mode = request.form.get("mode", "")
            current = parse_number(request.form.get("current_a", ""))
            comment = request.form.get("comment", "").strip()
            errors = validate_charge(database, battery_id, charged_on, capacity, mode, current)
            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template(
                    "charge_form.html", batteries=batteries, selected_battery_id=battery_id,
                    today=date.today().isoformat(),
                )
            duplicate_charge = database.execute(
                "SELECT 1 FROM charges WHERE battery_id = ? AND charged_on = ?",
                (battery_id, charged_on),
            ).fetchone()
            if duplicate_charge and request.form.get("confirm_duplicate") != "yes":
                return render_template(
                    "charge_form.html", batteries=batteries, selected_battery_id=battery_id,
                    today=date.today().isoformat(), duplicate_charge=True,
                )
            database.execute(
                "INSERT INTO charges (battery_id, charged_on, capacity_mah, mode, current_a, comment) VALUES (?, ?, ?, ?, ?, ?)",
                (battery_id, charged_on, capacity, mode, current, comment or None),
            )
            database.commit()
            flash("Laddningen är registrerad.", "success")
            return redirect(url_for("battery_detail", battery_id=battery_id))
        return render_template(
            "charge_form.html", batteries=batteries, selected_battery_id=selected_battery_id,
            today=date.today().isoformat(),
        )

    @app.post("/charges/<int:charge_id>/delete")
    def charge_delete(charge_id):
        database = get_db()
        charge = database.execute("SELECT battery_id FROM charges WHERE id = ?", (charge_id,)).fetchone()
        if charge is None:
            abort(404)
        database.execute("DELETE FROM charges WHERE id = ?", (charge_id,))
        database.commit()
        flash("Laddningen är borttagen.", "success")
        return redirect(url_for("battery_detail", battery_id=charge["battery_id"]))

    with app.app_context():
        initialize_database()
    return app


def get_db():
    if "db" not in g:
        database_path = g.get("database_path") or DATABASE_PATH
        database_path.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(database_path)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def initialize_database():
    schema_path = BASE_DIR / "schema.sql"
    database = get_db()
    database.executescript(schema_path.read_text(encoding="utf-8"))
    migrate_schema(database)
    database.commit()


def migrate_schema(database):
    columns = database.execute("PRAGMA table_info(charges)").fetchall()
    current_column = next((column for column in columns if column["name"] == "current_a"), None)
    if current_column is None or not current_column["notnull"]:
        return

    database.execute("BEGIN")
    try:
        database.execute("ALTER TABLE charges RENAME TO charges_legacy")
        database.execute(
            """
            CREATE TABLE charges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                battery_id INTEGER NOT NULL REFERENCES batteries(id) ON DELETE CASCADE,
                charged_on TEXT NOT NULL,
                capacity_mah REAL NOT NULL CHECK(capacity_mah >= 0),
                mode TEXT NOT NULL CHECK(mode IN ('Activate', 'Charge', 'Analysis')),
                current_a REAL CHECK(current_a IS NULL OR (current_a >= 0.1 AND current_a <= 2.0)),
                comment TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        database.execute(
            """
            INSERT INTO charges (id, battery_id, charged_on, capacity_mah, mode, current_a, comment, created_at)
            SELECT id, battery_id, charged_on, capacity_mah, mode, current_a, comment, created_at
            FROM charges_legacy
            """
        )
        database.execute("DROP TABLE charges_legacy")
        database.execute("CREATE INDEX idx_charges_battery_date ON charges(battery_id, charged_on DESC)")
    except sqlite3.Error:
        database.rollback()
        raise


def get_type_or_404(type_id):
    battery_type = get_db().execute("SELECT * FROM battery_types WHERE id = ?", (type_id,)).fetchone()
    if battery_type is None:
        abort(404)
    return battery_type


def get_battery_or_404(battery_id):
    battery = get_db().execute(
        """
        SELECT batteries.*, battery_types.code AS type_code, battery_types.name AS type_name
        FROM batteries JOIN battery_types ON battery_types.id = batteries.type_id
        WHERE batteries.id = ?
        """,
        (battery_id,),
    ).fetchone()
    if battery is None:
        abort(404)
    return battery


def get_type_fields(type_id):
    return get_db().execute(
        "SELECT * FROM battery_type_fields WHERE type_id = ? ORDER BY position, id", (type_id,)
    ).fetchall()


def get_type_battery_count(type_id):
    return get_db().execute("SELECT COUNT(*) FROM batteries WHERE type_id = ?", (type_id,)).fetchone()[0]


def get_type_fields_with_usage(type_id):
    fields = get_type_fields(type_id)
    custom_values = get_db().execute(
        "SELECT custom_values FROM batteries WHERE type_id = ?", (type_id,)
    ).fetchall()
    values_by_field = []
    for field in fields:
        has_data = False
        for row in custom_values:
            values = json.loads(row["custom_values"] or "{}")
            value = values.get(field["field_key"])
            if value is not None and str(value).strip():
                has_data = True
                break
        values_by_field.append({**dict(field), "has_data": has_data})
    return values_by_field


def remove_custom_field_values(type_id, field_key):
    database = get_db()
    batteries = database.execute(
        "SELECT id, custom_values FROM batteries WHERE type_id = ?", (type_id,)
    ).fetchall()
    for battery in batteries:
        values = json.loads(battery["custom_values"] or "{}")
        if field_key in values:
            values.pop(field_key)
            database.execute(
                "UPDATE batteries SET custom_values = ? WHERE id = ?",
                (json.dumps(values, ensure_ascii=False), battery["id"]),
            )


def parse_custom_fields(labels, taken_keys=None):
    fields = []
    keys = set(taken_keys or [])
    for label in labels:
        label = label.strip()
        if not label:
            continue
        key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "falt"
        base_key = key
        number = 2
        while key in keys:
            key = f"{base_key}_{number}"
            number += 1
        keys.add(key)
        fields.append({"label": label, "key": key})
    return fields


def validate_type(database, code, name, type_id=None):
    errors = []
    if not code or not ID_PREFIX_PATTERN.fullmatch(code):
        errors.append("Typkod måste innehålla A–Z, siffror, bindestreck eller understreck.")
    if not name:
        errors.append("Namn på batteritypen krävs.")
    existing_type = database.execute("SELECT id FROM battery_types WHERE code = ?", (code,)).fetchone()
    if existing_type and existing_type["id"] != type_id:
        errors.append("Typkoden används redan.")
    return errors


def get_next_battery_sequence(battery_type):
    highest_number = 0
    number_width = 3
    pattern = re.compile(rf"^{re.escape(battery_type['code'])}-(\d+)$")
    identifiers = get_db().execute(
        "SELECT identifier FROM batteries WHERE type_id = ?", (battery_type["id"],)
    ).fetchall()
    for row in identifiers:
        match = pattern.fullmatch(row["identifier"])
        if match:
            number = int(match.group(1))
            highest_number = max(highest_number, number)
            number_width = max(number_width, len(match.group(1)))
    return f"{highest_number + 1:0{number_width}d}"


def build_battery_identifier(battery_type, sequence):
    sequence = sequence.strip()
    if not re.fullmatch(r"\d+", sequence):
        return None, "Löpnummer måste bestå av siffror."
    sequence_number = int(sequence)
    if sequence_number < 1:
        return None, "Löpnummer måste vara minst 1."
    number_width = len(get_next_battery_sequence(battery_type))
    return f"{battery_type['code']}-{sequence_number:0{number_width}d}", None


def parse_csv_text(csv_text, import_kind):
    cleaned_text = csv_text.lstrip("﻿").strip()
    if not cleaned_text:
        raise ValueError("Klistra in CSV-data innan du fortsätter.")
    if "	" in cleaned_text:
        parsed_rows = list(csv.reader(io.StringIO(cleaned_text), delimiter="	"))
    else:
        try:
            dialect = csv.Sniffer().sniff(cleaned_text[:4096], delimiters=";,|")
        except csv.Error:
            dialect = csv.excel
            dialect.delimiter = ";"
        parsed_rows = list(csv.reader(io.StringIO(cleaned_text), dialect))
    if not parsed_rows:
        raise ValueError("CSV-data saknar datarader.")
    headerless = import_kind == "charges" and is_import_date(parsed_rows[0][0] if parsed_rows[0] else "")
    if not headerless and len(parsed_rows) < 2:
        raise ValueError("CSV-data måste innehålla en rubrikrad och minst en datarad.")
    if headerless:
        headers = [
            "Datum", "Batteri-ID", "Uppmätt kapacitet (mAh)", "Laddningsläge",
            "Ström (A)", "Kommentar",
        ]
        rows = [row for row in parsed_rows if any(cell.strip() for cell in row)]
    else:
        headers = [header.strip() or f"Kolumn {index + 1}" for index, header in enumerate(parsed_rows[0])]
        rows = [row for row in parsed_rows[1:] if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError("CSV-data saknar datarader.")
    return headers, rows, headerless


def is_import_date(value):
    return any(
        try_parse_date(value, date_format) is not None
        for date_format in ("%Y-%m-%d", "%m/%d/%Y")
    )


def try_parse_date(value, date_format):
    try:
        return datetime.strptime(value.strip(), date_format)
    except (AttributeError, ValueError):
        return None


def normalize_import_date(value):
    for date_format in ("%Y-%m-%d", "%m/%d/%Y"):
        parsed_date = try_parse_date(value, date_format)
        if parsed_date is not None:
            return parsed_date.date().isoformat()
    return value.strip()


def get_import_options(import_kind, battery_type):
    if import_kind == "charges":
        return [
            ("ignore", "Ignorera kolumnen"),
            ("battery_identifier", "Batteri-ID"),
            ("charged_on", "Datum"),
            ("capacity_mah", "Uppmätt kapacitet (mAh)"),
            ("mode", "Laddningsläge"),
            ("current_a", "Ström (A)"),
            ("comment", "Kommentar"),
        ]
    options = [
        ("ignore", "Ignorera kolumnen"),
        ("identifier", "ID"),
        ("brand", "Märke"),
        ("chemistry", "Kemi"),
        ("voltage", "Spänning (V)"),
        ("country", "Tillverkningsland"),
        ("introduced_month", "Introduktionsmånad"),
        ("nominal_capacity_mah", "mAh (märkning)"),
        ("status", "Status"),
        ("latest_capacity_mah", "Senaste mAh (importerar mätning)"),
        ("last_charged", "Senast laddad (för importerad mätning)"),
    ]
    options.extend((f"custom:{field['field_key']}", field["label"]) for field in get_type_fields(battery_type["id"]))
    return options


def normalize_import_header(header):
    normalized = unicodedata.normalize("NFKD", header).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", normalized)


def suggest_import_mapping(headers, import_options):
    valid_targets = {option[0] for option in import_options}
    identifier_target = "battery_identifier" if "battery_identifier" in valid_targets else "identifier"
    header_aliases = {
        "id": identifier_target,
        "batteriid": identifier_target,
        "marke": "brand",
        "brand": "brand",
        "kemi": "chemistry",
        "chemistry": "chemistry",
        "v": "voltage",
        "volt": "voltage",
        "voltage": "voltage",
        "tillverkningsland": "country",
        "land": "country",
        "introduktionsmanad": "introduced_month",
        "mahmarkning": "nominal_capacity_mah",
        "marktkapacitet": "nominal_capacity_mah",
        "mahsenast": "latest_capacity_mah",
        "senastladdad": "last_charged",
        "datum": "charged_on",
        "uppmattkapacitet": "capacity_mah",
        "uppmattkapacitetmah": "capacity_mah",
        "kapacitet": "capacity_mah",
        "laddningslage": "mode",
        "strom": "current_a",
        "stroma": "current_a",
        "kommentar": "comment",
    }
    custom_headers = {
        normalize_import_header(label): target
        for target, label in import_options
        if target.startswith("custom:")
    }
    suggestions = {}
    for index, header in enumerate(headers):
        normalized_header = normalize_import_header(header)
        target = custom_headers.get(normalized_header, header_aliases.get(normalized_header, "ignore"))
        suggestions[index] = target if target in valid_targets else "ignore"
    return suggestions


def get_mapped_value(row, mapping, target):
    for index, mapped_target in mapping.items():
        if mapped_target == target:
            value = row[index].strip() if index < len(row) else ""
            return "" if value == "?" else value
    return ""


def validate_import_mapping(import_kind, mapping):
    selected_targets = [target for target in mapping.values() if target != "ignore"]
    duplicate_targets = {target for target in selected_targets if selected_targets.count(target) > 1}
    errors = [f"Fältet {target} är mappat från flera kolumner." for target in sorted(duplicate_targets)]
    required_targets = (
        {"identifier", "brand", "chemistry", "voltage", "introduced_month", "nominal_capacity_mah", "status"}
        if import_kind == "batteries"
        else {"battery_identifier", "charged_on", "capacity_mah", "mode"}
    )
    missing_targets = required_targets - set(selected_targets)
    if missing_targets:
        errors.append("Obligatoriska fält saknar mappning: " + ", ".join(sorted(missing_targets)) + ".")
    if import_kind == "batteries":
        measurement_targets = {"latest_capacity_mah", "last_charged"}
        selected_measurement_targets = measurement_targets & set(selected_targets)
        if selected_measurement_targets and selected_measurement_targets != measurement_targets:
            errors.append("Senaste mAh och Senast laddad måste mappas tillsammans.")
    return errors


def build_import_rows(database, import_kind, battery_type, headers, rows, mapping, row_number_start=2):
    errors = validate_import_mapping(import_kind, mapping)
    if errors:
        return [], errors, []
    import_rows = []
    warnings = []
    seen_identifiers = set()
    seen_charge_dates = set()
    for index, row in enumerate(rows, start=row_number_start):
        row_errors = []
        if import_kind == "batteries":
            identifier = get_mapped_value(row, mapping, "identifier").upper()
            brand = get_mapped_value(row, mapping, "brand")
            chemistry = get_mapped_value(row, mapping, "chemistry")
            voltage = parse_number(get_mapped_value(row, mapping, "voltage"))
            country = get_mapped_value(row, mapping, "country")
            introduced_month = get_mapped_value(row, mapping, "introduced_month")
            nominal_capacity = parse_integer(get_mapped_value(row, mapping, "nominal_capacity_mah"))
            status = get_mapped_value(row, mapping, "status")
            if not re.fullmatch(rf"{re.escape(battery_type['code'])}-\d+", identifier):
                row_errors.append(f"ID måste börja med {battery_type['code']}- och sluta med siffror.")
            if identifier in seen_identifiers:
                row_errors.append("ID förekommer flera gånger i importen.")
            seen_identifiers.add(identifier)
            row_errors.extend(
                validate_battery(
                    database, battery_type, identifier, brand, chemistry, voltage,
                    introduced_month, nominal_capacity, status, allow_unknown_introduced_month=True,
                )
            )
            custom_values = {
                field["field_key"]: get_mapped_value(row, mapping, f"custom:{field['field_key']}")
                for field in get_type_fields(battery_type["id"])
            }
            measurement = None
            if "latest_capacity_mah" in mapping.values():
                latest_capacity = parse_integer(get_mapped_value(row, mapping, "latest_capacity_mah"))
                last_charged = normalize_import_date(get_mapped_value(row, mapping, "last_charged"))
                try:
                    datetime.strptime(last_charged, "%Y-%m-%d")
                except ValueError:
                    row_errors.append("Senast laddad måste vara i formatet ÅÅÅÅ-MM-DD.")
                if latest_capacity is None or latest_capacity < 0:
                    row_errors.append("Senaste mAh måste vara noll eller ett positivt tal.")
                measurement = {
                    "charged_on": last_charged,
                    "capacity_mah": latest_capacity,
                    "mode": "Analysis",
                    "current_a": 0.1,
                    "comment": "Importerad senaste mätning",
                }
            import_rows.append(
                {
                    "row_number": index,
                    "label": identifier or "Saknat ID",
                    "summary": f"{brand or 'Saknat märke'} · {status or 'Saknad status'}",
                    "errors": row_errors,
                    "warnings": [],
                    "skip": False,
                    "duplicate": False,
                    "values": {
                        "type_id": battery_type["id"], "identifier": identifier, "brand": brand,
                        "chemistry": chemistry, "voltage": voltage, "country": country,
                        "introduced_month": introduced_month, "nominal_capacity_mah": nominal_capacity,
                        "status": status, "custom_values": custom_values, "measurement": measurement,
                    },
                }
            )
        else:
            identifier = get_mapped_value(row, mapping, "battery_identifier").upper()
            battery = database.execute("SELECT id FROM batteries WHERE identifier = ?", (identifier,)).fetchone()
            charged_on = normalize_import_date(get_mapped_value(row, mapping, "charged_on"))
            capacity = parse_integer(get_mapped_value(row, mapping, "capacity_mah"))
            mode = get_mapped_value(row, mapping, "mode")
            current = parse_number(get_mapped_value(row, mapping, "current_a"))
            comment = get_mapped_value(row, mapping, "comment")
            row_warnings = []
            if battery is None:
                row_warnings.append(f"Batteriet {identifier or 'utan ID'} finns inte och raden hoppas över.")
                battery_id = None
                skip = True
                duplicate = False
            else:
                battery_id = battery["id"]
                skip = False
                charge_key = (battery_id, charged_on)
                existing_charge = database.execute(
                    "SELECT 1 FROM charges WHERE battery_id = ? AND charged_on = ?",
                    charge_key,
                ).fetchone()
                duplicate = existing_charge is not None or charge_key in seen_charge_dates
                if existing_charge:
                    row_warnings.append(f"Batteriet har redan en laddning registrerad {charged_on}.")
                elif charge_key in seen_charge_dates:
                    row_warnings.append(f"Importen innehåller redan en laddning för batteriet {charged_on}.")
                seen_charge_dates.add(charge_key)
                row_errors.extend(
                    validate_charge(database, battery_id, charged_on, capacity, mode, current, allow_unknown_current=True)
                )
            import_rows.append(
                {
                    "row_number": index,
                    "label": identifier or "Saknat ID",
                    "summary": f"{charged_on or 'Saknat datum'} · {capacity if capacity is not None else '–'} mAh",
                    "errors": row_errors,
                    "warnings": row_warnings,
                    "skip": skip,
                    "duplicate": duplicate,
                    "values": {
                        "battery_id": battery_id, "charged_on": charged_on, "capacity_mah": capacity,
                        "mode": mode, "current_a": current, "comment": comment,
                    },
                }
            )
    for import_row in import_rows:
        for error in import_row["errors"]:
            errors.append(f"Rad {import_row['row_number']} ({import_row['label']}): {error}")
        for warning in import_row["warnings"]:
            warnings.append(f"Rad {import_row['row_number']} ({import_row['label']}): {warning}")
    return import_rows, errors, warnings


def commit_import_rows(database, import_kind, import_rows):
    for import_row in import_rows:
        if import_row["skip"]:
            continue
        values = import_row["values"]
        if import_kind == "batteries":
            cursor = database.execute(
                """
                INSERT INTO batteries
                (type_id, identifier, brand, chemistry, voltage, country, introduced_month,
                 nominal_capacity_mah, status, custom_values)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["type_id"], values["identifier"], values["brand"], values["chemistry"],
                    values["voltage"], values["country"] or None, values["introduced_month"],
                    values["nominal_capacity_mah"], values["status"],
                    json.dumps(values["custom_values"], ensure_ascii=False),
                ),
            )
            if values["measurement"]:
                measurement = values["measurement"]
                database.execute(
                    "INSERT INTO charges (battery_id, charged_on, capacity_mah, mode, current_a, comment) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        cursor.lastrowid, measurement["charged_on"], measurement["capacity_mah"],
                        measurement["mode"], measurement["current_a"], measurement["comment"],
                    ),
                )
        else:
            database.execute(
                "INSERT INTO charges (battery_id, charged_on, capacity_mah, mode, current_a, comment) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    values["battery_id"], values["charged_on"], values["capacity_mah"],
                    values["mode"], values["current_a"], values["comment"] or None,
                ),
            )
    database.commit()


def parse_number(value):
    try:
        return float(Decimal(value.strip().replace(",", ".")))
    except (AttributeError, InvalidOperation):
        return None


def parse_integer(value):
    try:
        parsed_value = Decimal(value.strip())
    except (AttributeError, InvalidOperation):
        return None
    return int(parsed_value) if parsed_value == parsed_value.to_integral_value() else None


def validate_battery(
    database, battery_type, identifier, brand, chemistry, voltage, introduced_month,
    nominal_capacity, status, allow_unknown_introduced_month=False,
):
    errors = []
    if identifier and database.execute("SELECT 1 FROM batteries WHERE identifier = ?", (identifier,)).fetchone():
        errors.append("Detta batteri-ID används redan.")
    if not brand:
        errors.append("Märke krävs.")
    if not chemistry:
        errors.append("Kemi krävs.")
    if voltage is None or voltage <= 0:
        errors.append("Ange en giltig spänning.")
    if introduced_month and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", introduced_month):
        errors.append("Introduktionsmånad ska anges som ÅÅÅÅ-MM.")
    elif not introduced_month and not allow_unknown_introduced_month:
        errors.append("Introduktionsmånad ska anges som ÅÅÅÅ-MM.")
    if nominal_capacity is None or nominal_capacity <= 0:
        errors.append("Ange en märkning i mAh som ett heltal större än noll.")
    if status not in {"Aktiv", "Väntande", "Ej aktivt"}:
        errors.append("Ogiltig status.")
    return errors


def validate_charge(database, battery_id, charged_on, capacity, mode, current, allow_unknown_current=False):
    errors = []
    if not battery_id or not database.execute("SELECT 1 FROM batteries WHERE id = ?", (battery_id,)).fetchone():
        errors.append("Välj ett giltigt batteri.")
    try:
        datetime.strptime(charged_on, "%Y-%m-%d")
    except (TypeError, ValueError):
        errors.append("Ange ett giltigt datum.")
    if capacity is None or capacity < 0:
        errors.append("Ange en uppmätt kapacitet som ett heltal på minst 0 mAh.")
    if mode not in {"Activate", "Charge", "Analysis"}:
        errors.append("Välj ett giltigt laddningsläge.")
    if current is None and not allow_unknown_current:
        errors.append("Ström ska vara mellan 0,1 och 2,0 A i steg om 0,1 A.")
    elif current is not None and (
        current < 0.1 or current > 2.0 or abs(current * 10 - round(current * 10)) > 0.00001
    ):
        errors.append("Ström ska vara mellan 0,1 och 2,0 A i steg om 0,1 A.")
    return errors


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=os.environ.get("FLASK_DEBUG") == "1")
