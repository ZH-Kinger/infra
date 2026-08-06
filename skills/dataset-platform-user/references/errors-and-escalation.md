# Errors and escalation

| Symptom | Classification | Safe action |
|---|---|---|
| Cannot see PAI Workspace | PAI membership | Request the appropriate workspace role through access IaC |
| API denied after login | RAM | Ask an administrator to inspect the task Role; do not request an AccessKey |
| DataFlow path not covered | Fileset/DataFlow | Escalate the exact CPFS path; do not switch to raw OSS training |
| `OperationDenied.InvalidState` | CPFS readiness | Check protocol service, filesystem, and preceding task terminal state |
| `_READY marker is missing` | Incomplete release | Stop; select a completed release or ask the data steward to republish |
| Commit or Manifest mismatch | Integrity | Stop; never bypass `training-guard` |
| PAI URI format error | Registration | Check DataSourceType and `cpfs://`/`nas://`/`bmcpfs://` scheme pairing |
| DSW/DLC mount failure | Topology or catalog | Check zone, vSwitch, protocol service, Dataset Version, and RO contract |
| Portal only saves a plan | Authorization | Execution requires a signed-in allowlisted administrator |

Escalation template:

```text
Intent: <adopt / train / preheat / lifecycle / audit>
Dataset: <short-name>
Commit or Tag: <immutable identifier>
Source: <non-secret OSS prefix or CPFS path>
Region: <region>
Workflow run: <URL or run ID>
Exact error: <redacted error text>
Requested admin action: <register source / add Fileset-DataFlow / grant PAI role / inspect mount>
```

Never include AccessKey, Secret, STS token, lakeFS credential, GitHub token, or Terraform state.
