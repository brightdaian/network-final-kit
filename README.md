# 기말 프로젝트 학생 키트 (v1.3)

| 파일 | 내용 |
|---|---|
| `CONTRACT.md` | **반드시 지켜야 할 API · 경보 · 상태 규칙.** 자동 채점은 이 문서만 확인한다 |
| `compose.template.yaml` | 내 저장소 루트에 `compose.yaml` 로 복사해서 쓰는 출발점 |
| `env.example` · `gitignore.example` · `gitattributes.example` | 저장소 루트에 **앞에 점을 붙여서** `.env.example` · `.gitignore` · `.gitattributes` 로 복사한다 |
| `grading.example.json` | 저장소 루트에 둘 `grading.json` 예시 |
| `sensor-sim/` | 센서 시뮬레이터 (코드 수정 금지, `sim.yaml` 만 바꾼다) |
| `grader/` | 공개 채점기 — 제출 전에 스스로 돌려 본다 |

## 프로젝트 저장소 구성 예

```
my-project/                 ← git 저장소
├── compose.yaml            ← compose.template.yaml 에서 시작
├── sim.yaml                ← 학번 · 기기 구성
├── grading.json            ← CONTRACT.md 0절
├── .env.example            ← 키트의 env.example 을 점 붙여 복사. .env 자체는 올리지 않는다
├── .gitattributes          ← * text=auto eol=lf
├── .gitignore
├── mosquitto/              ← mosquitto.conf · acl · entrypoint.sh
│                              (sensor-sim/mosquitto/secure/ 를 복사해서 수정)
├── sensor-sim/             ← 이 키트의 sensor-sim 을 그대로 복사
└── app/                    ← FastAPI 앱 (웹 화면 · REST · SSE)
```

### 예시 파일 복사 (점을 빼 두었다 — 탐색기에서 안 보이는 일을 막기 위해서다)

```bash
cp <키트 경로>/env.example           ./.env.example      # 점을 붙인다
cp <키트 경로>/gitignore.example     ./.gitignore
cp <키트 경로>/gitattributes.example ./.gitattributes
```

`.gitattributes` 를 빠뜨리면 **윈도우에서 줄바꿈이 CRLF 로 바뀌어 브로커가 뜨지 않는다.**
`.gitignore` 를 빠뜨리면 `.env` 가 저장소에 올라간다 (보안 감점). 둘 다 꼭 복사한다.

## 자가 채점

Python 3.10 이상과 Docker 만 있으면 된다 (추가 패키지 없음). 약 12분 걸린다.
Windows 는 아래 `python3` 를 `python` 으로 바꿔 친다. Windows · macOS 별 주의점은 안내서 3.11.

```bash
python3 <키트 경로>/grader/grade.py <내 저장소 경로> --sim-dir <키트 경로>/sensor-sim
```

결과는 내 저장소의 `grade-report/report.md` 에 생긴다 (`.gitignore` 에 `grade-report/` 와 `.grader-override.yaml` 포함).
채점기는 실행할 때 `docker compose down -v` 로 내 스택을 지우고 새로 띄운다 — 개발용 데이터는 날아간다.

### ★ 제출 전에 한 번은 "clone 한 사본" 으로 돌린다

평가자는 내 저장소를 **새로 clone 해서** `docker compose up -d --build` 한 번만 실행한다.
내 작업 폴더에만 있는 `.env` 나 손으로 만든 계정 파일에 의존하면 **기동 실패 = 자동 채점 0점**이다.

```bash
git clone <내 저장소> /tmp/check && python3 <키트>/grader/grade.py /tmp/check --sim-dir <키트>/sensor-sim
```

**실제 채점은 같은 항목을 다른 조건으로 돌린다.** 고장의 순서 · 시점 · 강도 · 대상 기기가 바뀌고, 비밀 값 때문에 방 이름 · 정원 · 이상치 시점도 바뀐다.
공개판 점수를 맞추는 요령이 아니라 CONTRACT.md 의 규칙을 그대로 구현하는 것이 유일한 방법이다.
