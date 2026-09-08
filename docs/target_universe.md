# Target Universe V1

The Target Universe records source boards and sites that appeared in historical operator job links. A historical occurrence proves only that a target appeared in prior sourcing; it does not prove that the board is active today.

`SearchBrief` answers what a client wants. The Target Universe answers which source targets JobSift has historical evidence for. A `SourcingPlan` selects a bounded set of those targets for one run. The universe is not itself a client plan and does not perform live health checks.

The offline builder accepts an `.xlsx` workbook or CSV export and writes a deterministic target artifact:

```bash
python -m validation.target_universe_v1.build --input operator-history.xlsx --output validation/target_universe_v1/historical_v1.json
```

Only four proven source contracts are converted to targets: Greenhouse, Ashby, Workday, and Lever. Other links, malformed URLs, and recognized provider URLs that lack safe coordinates are retained as classified counts with representative evidence. A later health-validation step may separately classify historical targets as active, empty, invalid, transiently failing, or unresolved.
