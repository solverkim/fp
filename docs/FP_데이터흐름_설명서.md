# Factory Planner (fp) 전체 데이터 흐름 설명서

> ERPNext 위에서 동작하는 지능형 생산 스케줄링 앱
> 경로: `/home/kjkim/frappe-bench/apps/fp/`
> 모듈: **Factory Planner**
> 작성일: 2026-04-08

---

## 0. 한눈에 보기

```
[ERPNext 원천 데이터]                    [FP 마스터 데이터]
  Sales Order                              FP TAT Master
  Stock Ledger                             FP Setup Matrix / Setup Group
  Work Order (firm)                        FP Shift Calendar
  BOM / Routing                            FP Solver Config
        │                                         │
        ▼                                         │
┌────────────────────┐                            │
│ ① 수요 입력         │  FP Demand Profile         │
│   (Demand Profile) │  + Demand Profile Item     │
└────────┬───────────┘                            │
         ▼                                         │
┌────────────────────┐                            │
│ ② Netting & Lot   │  netting.py                │
│   Sizing           │  lot_sizing.py              │
└────────┬───────────┘                            │
         ▼                                         │
┌────────────────────┐                            │
│ ③ Solver Input    │ ◄──────────────────────────┘
│   Loader           │  data_loader.py
└────────┬───────────┘
         ▼
┌────────────────────┐
│ ④ CP-SAT Engine   │  engine.py
│  (OR-Tools)        │  목적함수: α·지연 + β·셋업
└────────┬───────────┘
         ▼
┌────────────────────┐
│ ⑤ Result Writer   │  result_writer.py
└────────┬───────────┘
         ▼
┌────────────────────┐
│ FP Planning        │  status: Pre Plan
│  Snapshot          │       → Draft Plan
│  + Snapshot Job    │       → Fixed Plan
└────────┬───────────┘
         ▼
┌────────────────────┐
│ ⑥ Frozen Window   │  release.py (daily)
│   Release          │  → ERPNext Work Order 생성
└────────┬───────────┘
         ▼
┌────────────────────┐
│ ⑦ Daily Split     │  daily_split.py (daily)
│   (미달 수량 재투입)│  → 다음 Pre Plan 에 추가
└────────────────────┘
```

전체 흐름은 **수요(Demand) → 스케줄(Schedule) → 작업지시(Work Order)** 의 3단 파이프라인으로 구성됩니다.

---

## 1. 핵심 DocType 카탈로그

`fp/factory_planner/doctype/` 아래에 정의된 10개 DocType.

| 구분 | DocType | 역할 | 주요 필드 |
|------|---------|------|----------|
| 입력 | **FP Demand Profile** | 주간 수요 묶음 헤더 | `planning_week`, `items[]` |
| 입력 | FP Demand Profile Item | 품목별 수요 (자식) | `item_code`, `gross_demand`, `available_inventory`, `firm_wo_qty`, `lot_size`, `due_date` |
| 마스터 | **FP TAT Master** | 품목·공정·설비별 표준작업시간 | `item_code`, `operation`, `workstation`, `base_tat_mins`, `wait_time_mins`, `is_inline_inspection`, `inspection_tat_mins` |
| 마스터 | **FP Setup Matrix** | 설비 전환 시간 행렬 | `workstation`, `from_setup_group`, `to_setup_group`, `setup_time_mins`, `is_transition_allowed` |
| 마스터 | FP Setup Group | 셋업 그룹 정의 | `setup_group_name`, `items[]` |
| 마스터 | FP Setup Group Item | 그룹 소속 품목 | `item_code`, `item_name` |
| 마스터 | **FP Shift Calendar** | 일자·교대별 가용시간 | `workstation`, `date`, `shift_type`, `start_time`, `end_time`, `available_capacity_mins`, `is_holiday` |
| 설정 | **FP Solver Config** | 최적화 파라미터 | `alpha`(=1000), `beta`(=1), `max_time_secs`(=120), `enable_scip_ensemble`, `quality_threshold` |
| 결과 | **FP Planning Snapshot** | 스케줄 결과 헤더 | `snapshot_name`, `status`, `horizon_start/end`, `total_tardiness_mins`, `total_setup_time_mins`, `master_snapshot`(JSON) |
| 결과 | **FP Snapshot Job** | 스케줄된 작업 (자식) | `job_id`, `item_code`, `qty`, `operation`, `workstation`, `planned_start`, `planned_end`, `tardiness_mins`, `is_frozen`, `work_order` |

**Snapshot 상태 머신**
```
Pre Plan  ──(solver run)──▶  Draft Plan  ──(승인)──▶  Fixed Plan
```
- **Pre Plan**: 수요만 모인 상태 (스케줄 전)
- **Draft Plan**: Solver 결과가 작성된 상태 (검토/수정 가능)
- **Fixed Plan**: 확정 — Frozen Window가 여기서 Work Order를 만든다

---

## 2. 단계별 상세 + 샘플 데이터

이 절은 다음 시나리오 1건을 끝까지 추적합니다.

> **시나리오**: 2026-04-15 월요일에 `Item-X` 100개를 2026-04-20까지 납품해야 한다.
> 현재 재고 20, 진행 중 작업지시 10, 표준 LOT 50.

### Stage ① 수요 입력 — FP Demand Profile

ERP 사용자는 영업오더/MRP 결과를 보고 **FP Demand Profile** 헤더를 만들고, 자식 행에 품목을 입력합니다.

**샘플 — `FP Demand Profile (PP-2026W16)`**

| field | value |
|-------|-------|
| planning_week | `2026-W16` |
| horizon_start | `2026-04-13` |
| horizon_end | `2026-04-19` |

**자식 — `FP Demand Profile Item`**

| item_code | gross_demand | available_inventory | firm_wo_qty | lot_size | due_date |
|-----------|--------------|---------------------|-------------|----------|----------|
| Item-X    | 100          | 20                  | 10          | 50       | 2026-04-20 |

### Stage ② Netting & Lot Sizing

- 코드: `fp/demand/netting.py`, `fp/demand/lot_sizing.py`
- 핵심 함수: `compute_netting()`, `split_into_lots()`, `build_demand_profile()`

**식**
```
net_demand = gross_demand − available_inventory − firm_wo_qty
           = 100 − 20 − 10
           = 70
```

**Lot 분할 (FOQ, lot_size = 50)**
```
split_into_lots(70, 50)  →  [50, 20]
```

**산출 — 내부 Job 리스트** (메모리에서 다음 단계로 전달)

| job_id    | item_code | qty | due_date    | source_demand_id |
|-----------|-----------|-----|-------------|------------------|
| JOB-0001  | Item-X    | 50  | 2026-04-20  | PP-2026W16-1     |
| JOB-0002  | Item-X    | 20  | 2026-04-20  | PP-2026W16-1     |

이 Job 리스트가 Solver의 1차 입력이 됩니다.

### Stage ③ Solver Input Loader

- 코드: `fp/solver/data_loader.py` — `load_solver_inputs()`
- 역할: Job 리스트에 **TAT/BOM Routing/Setup/Shift** 마스터를 결합해 Solver가 먹을 수 있는 구조로 만든다.

**a) BOM Routing 조회** — `Item-X` 의 공정 순서를 ERPNext BOM에서 가져온다.

| seq | operation    |
|-----|--------------|
| 10  | Operation-A  |
| 20  | Operation-B  |

**b) FP TAT Master 조회**

| item_code | operation    | workstation | base_tat_mins | wait_time_mins |
|-----------|--------------|-------------|---------------|----------------|
| Item-X    | Operation-A  | WS-1        | 30            | 0              |
| Item-X    | Operation-B  | WS-1        | 60            | 5              |

**c) FP Setup Group / Setup Matrix**

```
Item-X  ∈  setup_group = "Group-A"

Setup Matrix (WS-1):
  Group-A → Group-A : 0 min
  Group-A → Group-B : 15 min
  Group-B → Group-A : 20 min
```

**d) FP Shift Calendar (WS-1)**

| date       | start | end   | available_capacity_mins | is_holiday |
|------------|-------|-------|-------------------------|------------|
| 2026-04-13 | 08:00 | 17:00 | 480                     | 0          |
| 2026-04-14 | 08:00 | 17:00 | 480                     | 0          |
| ...        | ...   | ...   | ...                     | ...        |

**e) Solver 입력 데이터 구조 (메모리)**

```python
Job(
  job_id="JOB-0001",
  item_code="Item-X",
  qty=50,
  due_date_mins=10080,         # horizon_start 부터 분(min) 환산
  operations=[
    Operation(seq=10, name="Operation-A",
              workstation="WS-1", tat_mins=30,  setup_group="Group-A"),
    Operation(seq=20, name="Operation-B",
              workstation="WS-1", tat_mins=60,  setup_group="Group-A",
              wait_after_mins=5),
  ],
)
```

마스터는 동시에 `master_snapshot` (JSON) 으로 직렬화되어 결과 Snapshot에 함께 저장됩니다 (감사 추적용).

### Stage ④ CP-SAT 최적화 엔진

- 코드: `fp/solver/engine.py` — `solve()`
- 라이브러리: **Google OR-Tools CP-SAT**, (옵션) SCIP 앙상블

**의사결정 변수**
- 각 (job, operation) 마다 `start_min`, `end_min`, `interval` 변수 생성
- `duration = tat_mins + setup_time + wait_time`

**제약**
1. **Precedence** — 같은 Job 안에서 Operation 순서 보장
   ```
   end(JOB-0001, Op-A)  ≤  start(JOB-0001, Op-B)
   ```
2. **NoOverlap** — 같은 Workstation 위 모든 interval은 겹칠 수 없음
3. **Setup-dependent transition** — 두 작업이 같은 WS에서 연속될 때 from→to setup_group 매트릭스 시간만큼 추가
4. **Inline inspection** — `is_inline_inspection=1` 이면 검사시간을 precedence delay 로 모델링
5. **Shift capacity** — Shift Calendar 의 available_capacity_mins 안에서만 작업 배치

**목적 함수 (FP Solver Config)**
```
minimize  α · Σ tardiness_i  +  β · Σ setup_time_j
        = 1000 · Σ tardiness_i + 1 · Σ setup_time_j
```
지연(tardiness) 가중치(α)가 셋업(β)보다 1000배 크므로, 납기 준수가 절대 우선.

**solve 결과 (`SolverResult`)**

| job_id   | operation   | workstation | start_min | end_min | tardiness_mins |
|----------|-------------|-------------|-----------|---------|----------------|
| JOB-0001 | Operation-A | WS-1        | 0         | 30      | 0              |
| JOB-0001 | Operation-B | WS-1        | 30        | 95      | 0              |
| JOB-0002 | Operation-A | WS-1        | 95        | 120     | 0              |
| JOB-0002 | Operation-B | WS-1        | 120       | 180     | 0              |

(min 단위는 horizon_start 기준 상대값)

### Stage ⑤ Result Writer — Planning Snapshot 저장

- 코드: `fp/solver/result_writer.py` — `write_snapshot()`
- 역할: 분(min) → 절대 datetime 변환, KPI 계산, DB 저장

**KPI 계산**
- **Total Tardiness** = Σ tardiness_mins
- **Total Setup Time** = Σ setup_time_mins
- **Line Utilization (%)** = (Σ processing_time / Σ capacity) × 100

**산출 — `FP Planning Snapshot (SNAP-2026W16-001)`**

| field | value |
|-------|-------|
| snapshot_name | `SNAP-2026W16-001` |
| status | `Draft Plan` |
| horizon_start | `2026-04-13 08:00` |
| horizon_end   | `2026-04-19 17:00` |
| total_tardiness_mins | `0` |
| total_setup_time_mins | `0` |
| line_utilization_pct | `4.7` |
| master_snapshot | `{ "tat_master": [...], "setup_matrix": [...], ... }` |

**자식 — `FP Snapshot Job`**

| job_id    | item_code | qty | operation    | workstation | planned_start       | planned_end         | tardiness_mins | is_frozen | work_order |
|-----------|-----------|-----|--------------|-------------|---------------------|---------------------|----------------|-----------|------------|
| JOB-0001  | Item-X    | 50  | Operation-A  | WS-1        | 2026-04-13 08:00    | 2026-04-13 08:30    | 0              | 0         | (null)     |
| JOB-0001  | Item-X    | 50  | Operation-B  | WS-1        | 2026-04-13 08:30    | 2026-04-13 09:35    | 0              | 0         | (null)     |
| JOB-0002  | Item-X    | 20  | Operation-A  | WS-1        | 2026-04-13 09:35    | 2026-04-13 10:00    | 0              | 0         | (null)     |
| JOB-0002  | Item-X    | 20  | Operation-B  | WS-1        | 2026-04-13 10:00    | 2026-04-13 11:00    | 0              | 0         | (null)     |

이 시점에서 사용자는 **Gantt Tuning** 페이지로 결과를 시각화/수동조정 한 뒤, 상태를 `Fixed Plan` 으로 승격합니다.

### Stage ⑥ Frozen Window Release (일배치)

- 코드: `fp/frozen_window/release.py`
- 스케줄: `hooks.py` 의 `scheduler_events.daily`
- 정책: 오늘로부터 **D+2** 일에 시작될 작업을 "동결"하여 ERPNext Work Order로 생성

**처리 흐름**
1. 가장 최근 `Fixed Plan` snapshot 조회
2. `planned_start` 가 `target_date(=today+2)` 인 Snapshot Job 필터
3. 각 Job 행 → `create_work_order_from_job()` 호출
4. Work Order 생성 후 `is_frozen=1`, `work_order=<WO 이름>` 역링크 저장

**샘플 — 2026-04-13 새벽 배치**

| FP Snapshot Job | Work Order |
|---|---|
| JOB-0001 / Op-A / WS-1 / start=2026-04-15 08:00 | `WO-2026-0001` |

**Work Order 매핑**

| Work Order field | 값 |
|------------------|---|
| production_item | Item-X |
| qty | 50 |
| planned_start_date | 2026-04-15 08:00 |
| expected_delivery_date | 2026-04-20 |
| custom_fp_snapshot_job | `SNAP-2026W16-001 → JOB-0001` |

→ Snapshot Job 의 `is_frozen` = 1, `work_order` = `WO-2026-0001`

### Stage ⑦ Daily Split (일배치)

- 코드: `fp/frozen_window/daily_split.py`
- 정책: 완료된 Work Order에서 **부족 수량(shortfall)** 이 있으면 자식 Job으로 만들어 다음 Pre Plan 에 자동 투입

**샘플**
- 2026-04-15 야간: `WO-2026-0001` 종료 → `produced_qty = 40`, `shortfall = 10`
- 자식 Job 생성:

| job_id          | item_code | qty | priority | parent_job |
|-----------------|-----------|-----|----------|------------|
| JOB-0001-SPLIT  | Item-X    | 10  | critical | JOB-0001   |

- 가장 최근 `Pre Plan` snapshot 의 jobs[] 에 append → 다음 `run_solver()` 실행 시 자동 반영

---

## 3. 실행 진입점 (Trigger Map)

| 트리거 | 함수 | 위치 |
|--------|------|------|
| 사용자 — "Run Planning" 버튼 | `run_planning()` | `solver/runner.py:200+` |
| 백그라운드 큐 (RQ) | `enqueue_solver()` | `solver/runner.py` |
| 메인 솔버 호출 | `run_solver(demand_jobs, horizon_start, horizon_end, snapshot_name)` | `solver/runner.py:56` |
| 데이터 로딩 | `load_solver_inputs()` | `solver/data_loader.py:21` |
| 최적화 | `solve()` | `solver/engine.py:505` |
| 결과 저장 | `write_snapshot()` | `solver/result_writer.py:23` |
| 일일 동결 (cron) | `release_frozen_window_orders()` | `frozen_window/release.py:15` |
| 일일 분할 (cron) | `process_daily_split()` | `frozen_window/daily_split.py:14` |

진행률은 Frappe Realtime 이벤트로 송출되어 UI에 표시됩니다 (`runner.py:32-48`).

---

## 4. UI 페이지

| Page | 경로 | 용도 |
|------|------|------|
| **planning_dashboard** | `factory_planner/page/planning_dashboard` | 두 Snapshot(A/B) 비교 — KPI(지연, 셋업, 가동률) 차이 시각화 |
| **gantt_tuning** | `factory_planner/page/gantt_tuning` | Pre/Draft Plan을 Gantt 로 시각화, 드래그로 수동 조정 후 Fixed로 승격 |
| **wo_tracking** | `factory_planner/page/wo_tracking` | Frozen 처리된 Work Order 의 진행 상태 추적 (Not Released / In Process / Completed) |

---

## 5. 종단(End-to-End) 요약 시퀀스

```
[T0  09:00] 사용자가 PP-2026W16 (Demand Profile) 작성, 저장
                 │
[T1  09:01] "Run Planning" 클릭
                 │
                 ▼
            netting → [JOB-0001(50), JOB-0002(20)]
                 │
                 ▼
            data_loader → Job + TAT + Setup + Shift 결합
                 │
                 ▼
            engine.solve()  (CP-SAT, ≤120s)
                 │
                 ▼
            result_writer → SNAP-2026W16-001 (Draft Plan)
                 │
[T2  09:05] 사용자가 Gantt Tuning 검토 후 Fixed Plan 으로 승격
                 │
[T3  D+2 02:00] cron: release_frozen_window_orders()
                 │      → WO-2026-0001 (Item-X × 50) 생성
                 │      → Snapshot Job.is_frozen = 1
                 │
[T4  생산 종료]    cron: process_daily_split()
                        produced_qty 부족분 → JOB-0001-SPLIT
                        → 다음 Pre Plan 에 투입 → 다음 run_solver() 에서 재배치
```

---

## 6. 파일 인덱스 (빠른 점프)

| 영역 | 파일 | 핵심 |
|------|------|------|
| 진입점 | `fp/solver/runner.py` | `run_solver`, `enqueue_solver`, `run_planning` |
| 입력 로더 | `fp/solver/data_loader.py` | `load_solver_inputs`, `build_master_snapshot` |
| 최적화 | `fp/solver/engine.py` | `solve`, `_build_cpsat_model`, `_extract_solution` |
| 결과 | `fp/solver/result_writer.py` | `write_snapshot`, `_calculate_utilization` |
| 수요 | `fp/demand/netting.py` | `compute_netting`, `split_into_lots`, `build_demand_profile` |
| LOT | `fp/demand/lot_sizing.py` | `fixed_order_quantity`, `lot_for_lot` |
| 동결 | `fp/frozen_window/release.py` | `release_frozen_window_orders`, `create_work_order_from_job` |
| 분할 | `fp/frozen_window/daily_split.py` | `process_daily_split`, `add_to_demand_pool` |
| 훅/스케줄 | `fp/hooks.py` | `scheduler_events.daily` |
| DocType | `fp/factory_planner/doctype/fp_*/` | 10개 DocType JSON + Python controller |
| UI | `fp/factory_planner/page/{planning_dashboard,gantt_tuning,wo_tracking}` | 대시보드 / 간트 / WO 추적 |

---

## 7. 알아두면 좋은 디자인 포인트

1. **마스터 스냅샷 동봉**: Solver 실행 시 사용된 TAT/Setup/Shift 마스터 전체를 JSON 으로 Planning Snapshot에 함께 저장 → 사후에 마스터가 바뀌어도 동일 결과 재현 가능 (감사 추적).
2. **분(min) 단위 좌표계**: Solver 내부에서는 모든 시간이 `horizon_start` 로부터의 분(min). 외부 표시 직전에만 datetime 으로 변환.
3. **2단계 솔버 (옵션)**: CP-SAT 결과가 OPTIMAL 이 아니면 SCIP 앙상블로 warm-start 재정제 (`enable_scip_ensemble`).
4. **목적함수 가중치**: α(지연)=1000, β(셋업)=1 — 납기 준수가 절대 최우선.
5. **상태 머신**: Pre Plan → Draft Plan → Fixed Plan. Fixed 가 되어야만 Frozen Window 가 Work Order 로 내려보낸다.
6. **Daily Split 자기치유**: 미달 수량이 자동으로 다음 사이클의 수요 풀에 들어가 우선순위 critical 로 재계획됨.
