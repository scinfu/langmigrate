## 2025-05-22 - [Visual History Table]
**Learning:** For a CLI tool managing a DAG (like database migrations), a structured table is significantly more readable than a simple list, especially when multiple heads or complex parent-child relationships are involved.
**Action:** Use `rich.Table` for any CLI output that presents relational or structured data to provide clear visual alignment and hierarchy.
