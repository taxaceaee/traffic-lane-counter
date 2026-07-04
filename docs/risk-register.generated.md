# Risk Register — TrafficFlow Vehicle Counting

## Metadata
- **Product**: TrafficFlow
- **Repository**: vehicle_counting
- **Commit SHA**: cbd7953d0e78cc8af2098725f08875bbc79465d6
- **Environment**: Local dev (SQLite in-memory)
- **Generated at**: 2026-07-02
- **Reviewer**: Enterprise Testing Agent

## Severity Policy
- **P0**: Exploitable security issue, data loss, active outage risk, release-stopping
- **P1**: Serious regression, high-risk vulnerability, critical workflow failure, missing mandatory gate
- **P2**: Important weakness requiring owner and risk acceptance before release
- **P3**: Minor hardening, documentation, or usability follow-up

## Risks

| ID | Severity | Area | Risk | Evidence | Impact | Mitigation | Status |
|---|---|---|---|---|---|---|---|
| R-001 | P3 | Security | `xml.etree` used in `evaluation/detrac_xml_parser.py` (S314) | ruff S314 finding | Limited — only processes internal DETRAC dataset XMLs | Replace with `defusedxml` | Open |
| R-002 | P3 | Code Quality | `B904` — 15 instances of `raise` without `from err` in except blocks | ruff B904 | Exception chain loss, harder debugging | Add `from err` to all HTTPException re-raises | Open |
| R-003 | P3 | Code Quality | `B008` — `Depends()` in argument defaults in routes_admin.py | ruff B008 | FastAPI still handles it correctly, but violates best practice | Use `Depends` as default parameter properly | Open |
| R-004 | P3 | Code Quality | `SIM115` — `open()` without context manager in routes_lanes.py | ruff SIM115 | File handle leak if exception occurs | Use `with open(...)` | Open |
| R-005 | P3 | Code Quality | Various unused imports (F401), import sorting (I001) | ruff 57 fixable issues | Code hygiene | Run `ruff check --fix` | Open |
| R-006 | P3 | Test Coverage | `backend/tracking/bytetrack_adapter.py` at 0% coverage | Coverage report | Tracking adapter not tested | Add unit tests | Open |
| R-007 | P3 | Test Coverage | `backend/visualization/visualizer.py` at 12% coverage | Coverage report | Visualizer not well tested | Add unit tests | Open |
| R-008 | P3 | Test Coverage | `backend/storage/retention.py` at 0% coverage | Coverage report | Retention policy not tested | Add unit tests | Open |
| R-009 | P3 | Test Coverage | `backend/storage/local_crop_storage.py` at 26% coverage | Coverage report | Crop storage not well tested | Add unit tests | Open |
| R-010 | P3 | Test Coverage | `backend/pipeline.py` at 17% coverage | Coverage report | Pipeline core not well tested | Add unit tests | Open |
| R-011 | P3 | Test Coverage | Overall 61% coverage, missing model serving and E2E paths | Coverage report | Broaden integration/E2E coverage | Add integration tests | Open |

## Release-Blocking Risks
**None.** No P0 or P1 risks identified. All critical security fixes (path traversal, CORS, API key auth, JWT auth) are implemented and tested.

## Accepted Risks (P2)
**None.** All identified issues are P3 (minor hardening/code quality).

## Follow-Up Hardening
1. Run `ruff check --fix` to auto-fix 57 fixable issues (import sorting, unused imports, etc.)
2. Add unit tests for `bytetrack_adapter.py`, `visualizer.py`, `retention.py`, `local_crop_storage.py`, `pipeline.py`
3. Replace `xml.etree` with `defusedxml` in `evaluation/detrac_xml_parser.py`
4. Add `B904` `from err` to HTTPException re-raises
5. Convert `SIM115` `open()` calls to context managers
