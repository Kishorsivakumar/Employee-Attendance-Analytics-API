# DECISIONS.md

## 1. Indexes and Query Performance

I created indexes based on the API's access patterns. A unique index on `employees.emp_code` prevents duplicate employees, while a unique compound index on `attendance_logs (emp_code, date)` prevents duplicate daily attendance records. Additional indexes support department-based employee queries, joining-date filters, attendance date ranges, status filtering, and late-arrival analytics. These indexes reduce unnecessary document scans and improve query performance.

## 2. Concurrent Punch-ins

I use the unique compound index on `(emp_code, date)` as the final safeguard against duplicate punch-ins. When simultaneous requests attempt to create attendance records for the same employee and date, MongoDB allows only one insert. The API catches duplicate-key errors and returns HTTP 409 for conflicting requests.

## 3. Leaderboard Ties

I use MongoDB's `$rank` aggregation operator to assign equal ranks to employees with the same late-arrival totals. The requested limit is applied by rank, so employees sharing the last eligible rank are retained. Employee code provides deterministic ordering when displaying tied employees.

## 4. Employees Without Attendance Logs

The department summary starts from eligible employee records and joins attendance logs using `$lookup`. This ensures employees who have joined by the reporting period's end are included in headcount even when they have no attendance records. Future joiners are excluded.

## 5. Scaling and Performance

I use MongoDB aggregation pipelines for analytics to reduce application-side processing and data transfer. I validate query plans using the explain endpoint and test concurrency and business rules with integration tests. For larger datasets, I would monitor execution plans, index efficiency, query latency, and resource usage, then optimize based on measured bottlenecks.