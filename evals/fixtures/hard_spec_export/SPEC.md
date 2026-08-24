# CSV export format

The exporter must produce output that our downstream loader accepts.

1. The first line is a header row: `id,name,notes`.
2. Columns are always emitted in that order, regardless of dict key order.
3. A field is quoted with double quotes **only if** it contains a comma, a double
   quote, or a newline. Unnecessary quoting breaks the loader's type sniffing.
4. A literal double quote inside a field is escaped by doubling it.
5. A missing field is written as an empty string, not the text `None`.
6. Rows are terminated with `\n`, including the final row.
