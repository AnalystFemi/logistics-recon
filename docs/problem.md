# Problem Statement

A third-party logistics (3PL) company runs three Warehouse
Management Systems (WMS) plus one central Order Management
System (OMS). Inventory movements don't sync fully, so the
systems disagree about how much stock exists.

**Goal:** find where and why the records differ, and give
transaction-level evidence to support inventory adjustments.

## Systems
- WMS-A, WMS-B, WMS-C: warehouse-level stock and movements
- OMS: central view of orders and available stock

## Discrepancy types
1. Missing: a transaction exists in one system but not another
2. Duplicated: the same transaction recorded twice
3. Delayed: synced late, so snapshots differ temporarily
4. Incorrect: wrong SKU, lot, quantity or unit of measure
5. Physical loss: records agree but real stock is lower

## Success criteria
- Every discrepancy is traced to a transaction and a cause
- System errors are separated from physical losses
- Checks run automatically and report exceptions