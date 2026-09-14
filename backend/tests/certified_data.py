"""Synthetic reproduction of the certified 120/110/90-row business scenarios."""

import csv
import io

COLUMNS = ["transaction_id", "transaction_date", "customer_id", "customer_name", "document_id",
           "email", "city", "product_id", "product_name", "quantity", "unit_price", "total_amount",
           "currency", "payment_method", "status", "source_system"]


def original_rows():
    rows = [{
        "transaction_id": f"TX{i:05}", "transaction_date": "2026-09-01",
        "customer_id": f"C{i:04}", "customer_name": f"Cliente {i % 43}",
        "document_id": "001234567" if i == 1 else "1234567", "email": f"cliente{i}@example.test",
        "city": "Bogotá", "product_id": "P001", "product_name": "Servicio",
        "quantity": "1", "unit_price": "100.00", "total_amount": "100.00",
        "currency": "COP", "payment_method": "TRANSFER", "status": "PAID", "source_system": "DEMO",
    } for i in range(1, 121)]
    rows[11]["customer_name"] = "  Cliente 12  "
    for i in [110, 111, 112, 113]:
        rows[i]["email"] = None
    rows[114]["transaction_id"] = rows[115]["transaction_id"] = "TX00115"
    rows[116]["transaction_id"] = rows[117]["transaction_id"] = "TX00117"
    rows[118]["quantity"] = "-1"
    rows[119]["total_amount"] = "-10"
    return rows


def csv_bytes(rows, columns=None):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns or COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


INTAKE_CONFIG = {"required_columns": COLUMNS, "unique_columns": ["transaction_id"],
                 "numeric_columns": ["document_id", "quantity", "unit_price", "total_amount"],
                 "positive_columns": ["quantity", "unit_price", "total_amount"], "max_error_rate": .05}
MONITOR_CONFIG = {"required_columns": COLUMNS,
                  "null_columns": ["customer_id", "email", "city", "transaction_id", "document_id", "currency"],
                  "max_null_rate": 0, "max_volume_change_pct": 15, "max_age_hours": 48}
