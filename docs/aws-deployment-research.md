# Deploying AI Ops Copilot on AWS — production architecture research

**Date of research:** 2026-08-13. Prices are `us-east-1` on-demand unless stated.
**Status:** research only. Nothing in this document has been built, and no
infrastructure code exists in this repository yet (there is no `Dockerfile`,
no `infra/`, no Terraform).

---

## 0. What is actually being deployed

Measured from the repository, not assumed:

| Artefact | Size | Notes |
|---|---|---|
| `data/index/vectors.npy` | 2.3 MB | 1,566 × 384 float32 |
| `data/index/chunks.pkl` | 1.3 MB | chunk text + metadata |
| `data/aiops.db` | 84 KB | SQLite: catalog, audit, escalations |
| `data/` total | 7.5 MB | includes corpus + LogHub samples |
| `bge-small-en-v1.5` (ONNX) | ~130 MB | downloaded by FastEmbed at first use |
| `ms-marco-MiniLM-L-12-v2` (ONNX) | ~120 MB | cross-encoder reranker |

Two processes: `uvicorn aiops.api.server:app` (FastAPI) and
`streamlit run src/aiops/ui/app.py`. Reranking is ~2.4–2.9 s/query on CPU and
is the dominant in-process latency. Synthesis calls the Anthropic API.

**The whole retrieval state is 3.6 MB.** Hold that number; it decides sections 3
and 9.

---

## 1. Compute: ECS Fargate vs App Runner vs EKS vs Lambda

### 1.1 App Runner is off the table — it is being retired

This is the single most important 2026 fact for this decision. AWS moved App
Runner to **maintenance mode effective 30 April 2026**: no new customers, no new
features, existing customers may continue.

- [InfoQ — AWS Ends WorkMail and Moves App Runner to Maintenance Mode](https://www.infoq.com/news/2026/04/aws-deprecates-workmail-apprunne/)
- [hashicorp/terraform-provider-aws#47161 — Service Deprecation: AWS App Runner](https://github.com/hashicorp/terraform-provider-aws/issues/47161)
- [Encore — The End of AWS App Runner](https://encore.dev/articles/end-of-app-runner)

AWS's own recommended migration target is **Amazon ECS Express Mode**, launched
21 Nov 2025, which gives App Runner's "hand me an image, get an HTTPS URL"
ergonomics on top of standard ECS/Fargate:

- [AWS — Announcing Amazon ECS Express Mode](https://aws.amazon.com/about-aws/whats-new/2025/11/announcing-amazon-ecs-express-mode/)
- [AWS re:Post — Launch web applications in seconds with Amazon ECS Express Mode](https://repost.aws/articles/ARDZrGhYT1SMCAeGbojOMbsg/re-invent-2025-launch-web-applications-in-seconds-with-amazon-ecs-express-mode)

Express Mode takes a container image + two IAM roles and provisions Fargate, an
ALB, HTTPS, a generated domain, and autoscaling. It **shares one ALB across up
to 25 services** via host-header rules, which matters here because this project
wants two front doors (API + Streamlit) and would otherwise pay for two ALBs.
There is **no Express Mode surcharge** — you pay only for the underlying
resources. Everything it creates stays visible and editable in your account, so
it is not a one-way door.

**Recommendation: do not build on App Runner in 2026.** If you want the
low-ceremony path, use ECS Express Mode; if you want explicit control, use plain
ECS Fargate. Either way the runtime is Fargate.

### 1.2 Lambda (container image) — technically possible, wrong shape

The 10 GB image limit is not the binding constraint. A `python:3.11-slim` +
`onnxruntime` + `fastembed` + `streamlit` + `langgraph` image lands around
1.5–2.5 GB, well inside the limit
([AWS Lambda container images guide](https://viprasol.com/blog/aws-lambda-container/)).
The real problems are:

1. **Cold start = image pull + 250 MB of ONNX model load into ORT sessions.**
   Lambda's own guidance is that a large image only hurts if the init path
   actually touches it — and here it does: the init path is exactly "load two
   ONNX models". The community benchmark work is explicit that "if your app
   imports a large dependency tree, scans files, loads models, or reads many
   assets during init, the image contents can absolutely affect cold start time"
   ([AJ Stuyvenberg — The case for containers on Lambda](https://aaronstuyvenberg.com/posts/containers-on-lambda)).
2. **Multi-second CPU inference per request** with a 15-minute hard cap and
   per-ms billing means you pay full compute price for 2.4 s of reranking on
   every invocation, with no amortisation across requests.
3. **Streamlit is a long-lived WebSocket server.** It is not a Lambda workload
   at all. You would end up splitting the deployment anyway.
4. Fixing cold starts with **Provisioned Concurrency** removes the only reason
   you chose Lambda (scale to zero) and reintroduces a fixed monthly floor.

Guidance from 2026 comparisons is consistent: "Lambda works for small model
inference (1–2 GB), but larger models need Fargate", and Lambda "is not suitable
for background tasks like ML inference servers"
([Serverless vs Containers: AWS Lambda vs ECS in 2026](https://www.cloudlaya.com/blog/serverless-vs-containers/),
[jayendrapatil — Lambda vs Fargate vs App Runner](https://jayendrapatil.com/aws-lambda-vs-fargate-vs-app-runner/)).

**Verdict: no.** Lambda would be defensible only if traffic were genuinely
bursty-to-zero *and* the reranker were dropped.

### 1.3 EKS — real cost, no benefit at this size

EKS adds a **$0.10/hour control-plane fee ≈ $73/month per cluster** before a
single container runs, and rises to $0.60/hour in extended support for old
Kubernetes versions
([CloudZero — EKS pricing](https://www.cloudzero.com/blog/eks-pricing/),
[Atmosly — Amazon EKS Pricing Explained 2026](https://atmosly.com/blog/eks-pricing)).
ECS has no control-plane charge. For one Python service with two front doors,
Kubernetes buys nothing this project needs and costs an extra $73/month plus
the operational surface of a cluster.

**Verdict: no**, unless the org already runs EKS and this is one more Deployment
in an existing cluster — in which case the $73 is already sunk and EKS is fine.

### 1.4 ECS Fargate — the mainstream 2026 choice

Fargate is the default for containerised CPU ML inference services in 2026: no
instance management, no control-plane fee, no cold start once a task is running,
full control over the runtime, and it is the substrate both App Runner and ECS
Express Mode were built on. AWS itself launched **18.4 million Fargate tasks per
day during Prime Day 2025**
([arXiv — Seekable OCI: Lazy-Loading Container Images](https://arxiv.org/html/2607.06868v1)).

The one Fargate weakness — task launch takes tens of seconds because of the
image pull — is directly addressed by **Seekable OCI (SOCI) lazy loading**,
which pulls only the bytes needed to start and streams the rest on demand.
Reported pull-time reductions are **7–9× (≈2.8 s vs 20–25 s)** and, critically,
**SOCI pull time is roughly independent of image size**:

- [AWS — Fargate Enables Faster Container Startup using Seekable OCI](https://aws.amazon.com/blogs/aws/aws-fargate-enables-faster-container-startup-using-seekable-oci/)
- [AWS Containers blog — Under the hood: Lazy Loading Container Images with SOCI and Fargate](https://www.amazonaws.cn/blog-selection/under-the-hood-lazy-loading-container-images-with-seekable-oci-and-aws-fargate/)

Note the interaction with §2: SOCI helps the *pull*; it does not help the *model
load*. If the models are baked into the image, SOCI will lazily fetch those
layers the moment ORT opens the files, so the win is smaller than the headline.

Run it on **Graviton (arm64)**: Fargate ARM tasks cost **~20% less** for the same
vCPU/memory, and the change is `runtimePlatform.cpuArchitecture: ARM64` plus a
multi-arch build
([AWS — Graviton2 support for Fargate](https://aws.amazon.com/blogs/aws/announcing-aws-graviton2-support-for-aws-fargate-get-up-to-40-better-price-performance-for-your-serverless-containers/),
[nOps — Are you missing out on AWS Graviton cost savings?](https://www.nops.io/blog/are-you-missing-out-on-aws-graviton-cost-savings/)).
ONNX Runtime is supported on Graviton and AWS publishes NLP-inference tuning
guidance for it ([AWS Graviton technical guide](https://aws.github.io/graviton/)).
**Benchmark the reranker on arm64 before committing** — the 20% discount is only
a win if per-query latency does not regress more than 20%.

### 1.5 Compute recommendation

> **ECS Fargate on Graviton (arm64), behind one ALB, two services (API +
> Streamlit) in one ECS cluster.** Use **ECS Express Mode** if you want the ALB,
> HTTPS, autoscaling, and listener rules provisioned for you and are happy with
> its defaults; use a hand-written ECS service + ALB if you want explicit
> control of health checks, sticky sessions, and deployment strategy.

Sizing: 1 task at **2 vCPU / 4 GB** per service. At 1,000 queries/day (~42/hour,
~0.7/minute) and 2.4 s of CPU per query, utilisation is under 3% — one task is
ample. Two tasks per service only for AZ redundancy, and that doubles the
compute line.

Streamlit specifics that will bite otherwise:
- ALB health check path must be **`/_stcore/health`** (append the base URL path
  if you set one).
- Streamlit's `/_stcore/stream` is a WebSocket upgrade; ALB supports it, and
  **WebSocket connections are inherently sticky once upgraded** — the target
  that returns HTTP 101 owns the connection. Enable target-group stickiness
  anyway so the pre-upgrade HTTP requests land on the same task.
- [Streamlit forum — Health check for Streamlit on ECS/Fargate](https://discuss.streamlit.io/t/health-check-for-streamlit-app-running-on-ecs-fargate/20277)
- [AWS — Sticky sessions for Application Load Balancers](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/sticky-sessions.html)

---

## 2. Model artefact handling: baked vs S3 vs EFS

### What teams actually do in 2026

The industry has converged on **"lean image + fetch weights at runtime"** for
*large* models, and **"bake it in"** for *small* ones. Both patterns are
mainstream; the split is by size and update cadence, not by ideology.

- Baking makes the container self-contained and portable but produces large
  images, slow builds, and forces a full rebuild for any model change. It is
  "best for small models or quick prototypes where simplicity is the top
  priority" ([apxml — Strategies for Packaging ML Models in Docker](https://apxml.com/courses/docker-for-ml-projects/chapter-3-managing-ml-data-containers/packaging-models-images-volumes)).
- The lean-image + object-store fetch pattern is "explicitly recommended … and
  very common for production ML workloads", using an init step, sidecar, CSI
  volume, or model loader
  ([Google Cloud — Scalable AI starts with storage: model artifact strategies](https://cloud.google.com/blog/topics/developers-practitioners/scalable-ai-starts-with-storage-guide-to-model-artifact-strategies),
  [HackerNoon — Your 12GB ML Container Is a Cold-Start Tax](https://hackernoon.com/your-12gb-ml-container-is-a-cold-start-tax)).
- The 2025-era refinement is **Mountpoint for Amazon S3 CSI driver v2**, which
  streams weights from S3 into pod memory with shared node-level caching. That
  is an **EKS** feature and does not apply to Fargate
  ([Gary Stafford — Loading multi-gigabyte model weights for GPU inference on EKS](https://garystafford.medium.com/loading-multi-gigabyte-model-weights-for-gpu-inference-on-amazon-eks-8efa93631bba)).

### Applied to this project

250 MB of ONNX is **small** by these standards. The three options:

| Option | Image size | Cold start | Reproducibility | Verdict |
|---|---|---|---|---|
| **Bake into image** | +250 MB (~2 GB total) | Best — no network fetch at boot; SOCI amortises the pull | **Best** — image digest pins model bytes exactly | ✅ **Recommended** |
| Download from S3 at start | ~1.7 GB | Adds an S3 GET + disk write before ORT can open the file; adds a failure mode at boot | Good only if you version the S3 key and pin it | Reasonable second choice |
| EFS mount | smallest | Adds an EFS mount target per AZ, NFS latency on first read, ~$0.30/GB-month, and a second thing to keep in sync | Worst — mutable shared state outside the image | ❌ Not worth it here |

**Do not let FastEmbed download from HuggingFace at container start.** That is
the default behaviour and it is the worst of all worlds: an external dependency
on a third party in your boot path, no pinning, and a hard requirement for
internet egress (which forces the NAT-gateway decision in §5). Bake the ONNX
files into the image at build time and set FastEmbed's cache directory to that
path so it never reaches the network.

**Where S3 *does* earn its place here: the index, not the models.** `vectors.npy`
+ `chunks.pkl` change every time the corpus is re-ingested, and they are 3.6 MB.
Put those in S3 with a versioned key, fetch at startup through a **free S3
gateway VPC endpoint**, and you can re-index without rebuilding the image. That
is the correct split: **immutable model weights in the image, mutable index in
S3.**

---

## 3. Vector storage — the honest answer is "you do not need one"

### The arithmetic

1,566 chunks × 384 dims × 4 bytes = **2.4 MB**. A brute-force
`numpy` matmul over that is sub-millisecond. The README already states this and
it is correct. For calibration: OpenSearch Serverless "next generation" and
S3 Vectors are engineered for the **10 million to 2 billion vector** range
([AWS — S3 Vectors now GA with 40× the scale of preview](https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-s3-vectors-generally-available/)).
This corpus is **four orders of magnitude** below the point where any of them
starts to make sense.

### What the options would cost anyway

**Amazon S3 Vectors** (GA 2 Dec 2025, now in 31 regions) is the only option
whose pricing model does not punish a tiny corpus: $0.06 per logical GB-month
storage, $0.20/GB for PUTs, $2.50 per million query requests plus tiny
data-processed and data-returned charges
([AWS S3 pricing](https://aws.amazon.com/s3/pricing/),
[Amazon S3 Vectors pricing deep dive](https://murraycole.com/posts/aws-s3-vectors-pricing-deep-dive),
[AWS — S3 Vectors expands to 17 additional regions](https://aws.amazon.com/about-aws/whats-new/2026/03/s3-vectors-expands-17-regions)).
At 1,566 vectors and 30k queries/month that is **cents**. But its latency is
"~100 ms for frequent queries, under one second for infrequent ones" — i.e. it
would *add* 100 ms to a retrieval step that currently costs under 1 ms, in
exchange for scale you do not have.

**OpenSearch Serverless** materially improved in 2026: the **next-generation**
version went GA on **28 May 2026** with **no minimum OCU and scale-to-zero after
10 minutes idle**, versus the classic model's floor of 2 OCUs (~$700/month) for
the first collection in an account
([AWS — Next generation of Amazon OpenSearch Serverless now GA](https://aws.amazon.com/about-aws/whats-new/2026/05/amazon-opensearch-serverless-next-generation-generally-available/),
[AWS Big Data blog — The next generation of Amazon OpenSearch Serverless](https://aws.amazon.com/blogs/big-data/the-next-generation-of-amazon-opensearch-serverless-built-from-the-ground-up-for-agents/),
[AWS docs — Scale to zero for OpenSearch Serverless](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-scale-to-zero.html)).
That removes the historic "$700/month to store 2 MB" absurdity — but scale-to-zero
also means a **cold-start on the first query after 10 idle minutes**, which is
exactly the wrong trade for a system already fighting 2.4 s of rerank latency.

Independent cost modelling puts an *idle* 10M-vector workload at **$3/month on
S3 Vectors, $121 on OpenSearch Serverless NextGen, $529 on Aurora pgvector, $50
on Pinecone (floor)**
([Darryl Ruggles — The real cost of vector storage](https://darryl-ruggles.cloud/the-real-cost-of-vector-storage-s3-vectors-vs-opensearch-vs-pgvector-vs-pinecone/)).
Every one of those is a bill for capacity this project will not use.

**Aurora/RDS pgvector** is the least-bad managed option *if you are already
running Postgres for §4* — the marginal cost of a `vector` column is near zero
and it removes a separate system. But it also removes the hand-written BM25
hybrid index that the ablation table shows is worth **+0.063 recall**, the single
largest gain in the pipeline. You would be trading a measured win for
conventionality.

**Bedrock Knowledge Bases** is a managed end-to-end RAG service: ingestion,
chunking, embedding, vector store, and retrieval. Adopting it would delete
`ingestion/`, `embedding/`, and `retrieval/` — i.e. the parts of this project
that carry the engineering argument, including the section-aware chunking, the
swept hybrid weights, the multi-hop reference following, and the ablation
harness. Its **Rerank API** is separately priced at **$1 per 1,000 queries for
Amazon Rerank, $2 per 1,000 for Cohere Rerank 3.5**
([AWS — Bedrock Rerank API](https://aws.amazon.com/about-aws/whats-new/2024/12/amazon-bedrock-rerank-api-accuracy-rag-applications/),
[Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/)) — at 1,000
queries/day that is $30–60/month to replace a model you already run for free,
though it would eliminate the 2.4 s CPU cost. That specific swap is the *only*
managed-RAG component worth considering, and it is a latency decision, not a
retrieval-quality one.

### Recommendation

> **Keep the in-memory index. Load `vectors.npy` and `chunks.pkl` from S3 at
> container start.** Object storage cost is under $0.01/month; retrieval stays
> sub-millisecond; the BM25 half of the hybrid keeps working; the swap-to-Qdrant
> seam in `retrieval/index.py` stays available for the day the corpus reaches
> ~1M chunks.

**Flag: this project does not need a vector database.** Adopting one at 1,566
chunks would add a service, a schema, a sync job, a cold-start, and 100 ms of
latency to optimise something that is not the bottleneck. The bottleneck is the
cross-encoder. Say that in the interview and it is a stronger answer than
"we use OpenSearch".

---

## 4. Relational storage: SQLite will not survive multiple tasks

Correct problem statement. SQLite on a Fargate task's ephemeral filesystem is
per-task, non-durable, and lost on every deployment. Three tables need a real
database: the error-code catalog (read-mostly, ~73 rows), the query audit trail
(append-only, ~1,000 rows/day), and the escalation queue (read-write, low
volume).

| Option | Cost at low traffic | Fit | Notes |
|---|---|---|---|
| **RDS PostgreSQL `db.t4g.micro`, Single-AZ** | **~$11.68/mo** + ~$2.30 for 20 GB gp3 ≈ **$14/mo** | ✅ Best | SQLAlchemy already in `pyproject.toml`; the schema ports essentially unchanged. Graviton instance class. |
| Aurora Serverless v2 | $0.12/ACU-hour; scale-to-zero possible but storage bills regardless (~$5/mo for 50 GB) | ⚠️ Overkill | Scale-to-zero (GA Nov 2024) pauses compute after inactivity, but it re-warms on the next connection — a latency spike on the first query of the day. |
| DynamoDB on-demand | $1.25/M WRU, $0.25/M strongly-consistent RRU, $0 idle | ⚠️ Poor fit | ~$0.04/month at this volume, but the catalog is a **SQL join** and the whole design argument in the README is "a lookup does not need an agent, SQL answers it exactly". Rewriting that as single-table DynamoDB access patterns loses the argument. |

The comparative research is blunt about Aurora Serverless v2 at low steady
traffic: "Serverless v2 at $0.12/ACU-hr is significantly more expensive than
equivalent provisioned Aurora or RDS with RIs for steady workloads … at steady
load, provisioned instances with RIs are approximately 5–6× cheaper per hour"
([JusDB — RDS vs Aurora vs Serverless v2 cost comparison](https://www.jusdb.com/blog/aws-rds-vs-aurora-vs-serverless-cost-comparison),
[Usage.ai — Aurora Serverless v2 complete 2026 guide](https://www.usage.ai/blogs/aws/rds/aurora-serverless-v2/),
[Infratally — AWS RDS PostgreSQL pricing 2026](https://infratally.com/articles/aws-rds-pricing-explained-2026/),
[AWS — Aurora Serverless v2 supports scaling to zero](https://aws.amazon.com/about-aws/whats-new/2024/11/amazon-aurora-serverless-v2-scaling-zero-capacity)).

### Recommendation

> **RDS PostgreSQL `db.t4g.micro`, Single-AZ, 20 GB gp3, in a private subnet.**
> ~$14/month, operationally boring, and the SQLAlchemy layer barely changes.
> Move to Multi-AZ ($28/mo) only when someone is actually on call for it.

**Flag: this project does not need Aurora.** Aurora's value is high-throughput
reads, fast failover, and storage that scales to 128 TB. None of that describes
three tables and 1,000 writes a day.

Keep SQLite for local dev and CI — the SQLAlchemy abstraction makes that a
connection-string difference, and CI should not need a database container.

---

## 5. Secrets, config, and networking

### 5.1 Secrets Manager vs SSM Parameter Store

| | Secrets Manager | SSM Parameter Store |
|---|---|---|
| Price | **$0.40/secret/month** + $0.05/10k API calls | **Free** for standard params (up to 10,000); $0.05 each for advanced |
| Rotation | Native, with Lambda rotators for RDS/Redshift/DocumentDB | None — you build it |
| ECS integration | Native `secrets` block in task definition | Native `secrets` block in task definition |

Both inject into ECS task definitions identically, so the application never
calls an AWS API — the ECS agent resolves the value and sets the environment
variable
([Cloud Kiln — Managing secrets in ECS: Parameter Store vs Secrets Manager](https://cloudkiln.com/blog/ecs-secrets-management),
[FactualMinds — Secrets Manager vs Parameter Store](https://www.factualminds.com/blog/aws-secrets-manager-vs-parameter-store-when-to-use-which/)).

**Recommendation:**
- **`ANTHROPIC_API_KEY` → Secrets Manager.** $0.40/month buys audit logging,
  cross-account policy, versioning, and a rotation story for a credential that
  bills real money if leaked. This is the right place to spend forty cents.
- **RDS credentials → Secrets Manager**, managed by RDS so rotation is automatic
  (+$0.40/month).
- **Everything `AIOPS_*` → SSM Parameter Store (free).** `AIOPS_TOP_K`,
  `AIOPS_DENSE_WEIGHT`, `AIOPS_DOC_CHUNK_TOKENS`, `AIOPS_CONFIDENCE_THRESHOLD`,
  `AIOPS_OTLP_ENDPOINT` are tuning knobs, not secrets. Free, hierarchical
  (`/aiops/prod/...`), and changing one is a task-definition revision rather than
  an image rebuild.

Total secrets cost: **$0.80–0.85/month.**

### 5.2 Minimal sane VPC layout

```
VPC 10.0.0.0/16
├─ Public subnet  10.0.0.0/24   (AZ a)  → ALB
├─ Public subnet  10.0.1.0/24   (AZ b)  → ALB (ALB requires ≥2 AZs)
├─ Private subnet 10.0.10.0/24  (AZ a)  → Fargate tasks, RDS
└─ Private subnet 10.0.11.0/24  (AZ b)  → RDS standby / second task
```

Security groups, chained so nothing is open to the internet:
- `sg-alb` — inbound 443 from `0.0.0.0/0`
- `sg-app` — inbound 8000 (API) and 8501 (Streamlit) **from `sg-alb` only**
- `sg-rds` — inbound 5432 **from `sg-app` only**

### 5.3 Is a NAT gateway required? — the counterintuitive answer

A NAT gateway costs **$0.045/hour ($32.85/month) + $0.045/GB processed**, plus
$0.005/hour for its attached Elastic IP since Feb 2024
([Cloud Burn — AWS NAT Gateway pricing](https://cloudburn.io/blog/aws-nat-gateway-pricing),
[The Scale Factory — IPv4 costs on AWS](https://scalefactory.com/blog/2023/08/02/ipv4-costs-on-aws/)).

The usual advice is "replace NAT with VPC interface endpoints". **At this scale
that advice is wrong.** Interface endpoints cost **$0.01 per AZ per hour**
($7.30/AZ/month) plus $0.01/GB
([AWS PrivateLink pricing](https://aws.amazon.com/privatelink/pricing/)). To run
Fargate in private subnets with no NAT you need endpoints for `ecr.api`,
`ecr.dkr`, `logs`, `secretsmanager`, and `ssm` — five interface endpoints × 2 AZs
× $7.30 = **~$73/month**, which is *more than double* the NAT gateway. The
break-even is roughly 160 GB/month per service
([PCG — VPC endpoints explanation and cost comparison](https://pcg.io/insights/vpc-endpoints-explanation-and-cost-comparison/),
[DEV — How (not) to burn money on VPC endpoints](https://dev.to/aws-builders/how-not-to-burn-money-on-vpc-endpoints-so-you-dont-have-to-2f4p)).

Three viable layouts, cheapest first:

**(a) Public subnet + `assignPublicIp: ENABLED` + locked-down security group —
$3.65/month per task.** Fargate tasks get a public IP on the task ENI directly
(there is no host EC2), which is enough to pull from ECR and reach
`api.anthropic.com`. Because `sg-app` only accepts inbound from `sg-alb`, the
task is **not reachable from the internet** despite having a public IP. This is
a legitimate, widely used cost-sensitive pattern
([AWS re:Post — How to launch ECS Fargate without public IP](https://repost.aws/questions/QUvcwWlXxiT5qSq5-Gjxf0pg/how-to-to-launch-ecs-fargate-container-without-public-ip)).
Cost: $0.005/hr × 730 = **$3.65/month/task**. Add the **free S3 gateway
endpoint** so index downloads and ECR layer traffic (ECR layers live in S3) do
not traverse the IGW as billable transfer.

**(b) One NAT gateway in one AZ, shared — ~$33/month + data.** Tasks stay in
private subnets. Conventional, auditable, single point of failure in one AZ
(acceptable here). Add the free S3 gateway endpoint; it removes 95%+ of the
bytes from the NAT bill because ECR image layers are S3 objects
([DEV — NAT gateways killing your container costs: ECR VPC endpoints to the rescue](https://dev.to/hstiwana/nat-gateways-killing-your-container-costs-amazon-ecr-vpc-endpoints-to-the-rescue-21k5)).

**(c) Full private + 5 interface endpoints — ~$73/month.** Only justified if a
compliance rule forbids internet egress.

**There is a fourth option that removes the egress requirement entirely:**
switch synthesis from `api.anthropic.com` to **Amazon Bedrock**, which has a
PrivateLink interface endpoint, so "you can access Amazon Bedrock as if it were
in your VPC, without an internet gateway, NAT device, VPN, or Direct Connect"
([AWS — Protect your data using VPC and PrivateLink with Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/usingVPC.html),
[aws-samples/amazon-bedrock-vpc-endpoints](https://github.com/aws-samples/amazon-bedrock-vpc-endpoints)).
Claude Opus 5, Sonnet 5, and Haiku 4.5 are all available on Bedrock. Note the
SDK client differs (`AnthropicBedrockMantle`, model IDs prefixed `anthropic.`),
and Bedrock is partner-operated with its own pricing. This is a genuine
architectural option, not a footnote — but it is a two-endpoint bill (~$29/mo
for `bedrock-runtime` across 2 AZs) and it changes the code.

**Recommendation:** start with **(a)** — public subnets, public IP on the task
ENI, ALB-only ingress, free S3 gateway endpoint. It costs $3.65/month instead of
$33 or $73, and the security posture is equivalent as long as the security group
is right. Move to **(b)** the moment a reviewer objects to a public IP on a
task; the delta is ~$30/month and one Terraform change.

---

## 6. Observability: landing OpenTelemetry on AWS in 2026

The landscape changed significantly in 2026. There are now **three** ways, and
the newest one is the simplest.

### 6.1 Direct OTLP to CloudWatch / X-Ray — no collector

CloudWatch now supports OpenTelemetry natively across **all three signals**:
metrics (queryable with PromQL), logs (Logs Insights + LiveTail), and traces
(explorable with **Transaction Search**). CloudWatch added direct OTLP metric
ingestion in **April 2026**
([AWS docs — OpenTelemetry on CloudWatch](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OpenTelemetry-Sections.html),
[AWS X-Ray — OTLP endpoint](https://docs.aws.amazon.com/xray/latest/devguide/xray-opentelemetry.html)).

For traces, set:

```
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://xray.<region>.amazonaws.com/v1/traces
```

Requests are SigV4-signed with the task role. This project already ships
`opentelemetry-exporter-otlp-proto-http` and already reads `AIOPS_OTLP_ENDPOINT`
from config — so **this is a single environment variable plus an IAM policy**,
with no sidecar and no collector to operate.

### 6.2 ADOT collector sidecar

AWS Distro for OpenTelemetry is the CNCF collector plus AWS exporters (X-Ray,
EMF) and receivers, deployed as a **sidecar container in the same ECS task**
([AWS X-Ray — ADOT](https://docs.aws.amazon.com/xray/latest/devguide/xray-services-adot.html),
[OneUptime — How to use the ADOT collector](https://oneuptime.com/blog/post/2026-02-06-aws-distro-opentelemetry-adot-collector/view)).
Its value is fan-out and control: batching, tail sampling, attribute
redaction, and shipping the same spans to CloudWatch *and* Prometheus *and* a
third-party backend. Its cost is a second container in every task — more vCPU,
more memory, more to deploy.

### 6.3 CloudWatch Application Signals

Application Signals is the *consumption* layer, not a transport: it ingests OTLP
traces and gives service maps, SLOs, and dependency views. **Transaction Search**
is the span-level explorer. Both sit on top of either 6.1 or 6.2.

### 6.4 GenAI spans specifically

The OpenTelemetry **GenAI semantic conventions** are the standard here, and this
project already emits them (`gen_ai.request.model`, `gen_ai.usage.input_tokens`,
`gen_ai.response.finish_reasons`)
([OpenTelemetry blog — Inside the LLM Call: GenAI Observability with OpenTelemetry](https://opentelemetry.io/blog/2026/genai-observability/),
[Greptime — How OpenTelemetry traces LLM calls, agent reasoning, and MCP tools](https://greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions)).

CloudWatch has a dedicated **GenAI Observability** console section, currently in
**preview**, giving a view of an AI application's health, performance, and
accuracy — and AWS's own Bedrock AgentCore documentation instructs you to
instrument with the ADOT SDK to feed it
([AWS docs — Add observability to Bedrock AgentCore resources](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)).
The README's note that the conventions are still in Development status remains
correct as of mid-2026 — keeping the attribute keys centralised in
`observability/tracing.py` is the right hedge and should stay.

**Cost:** Transaction Search bills per GB of span data — **$0.35/GB for the
first 10 TB** — with **1% of spans indexed free**, and $0.75 per million indexed
spans above that. X-Ray direct is $5.00 per million traces recorded, with
100,000/month always free
([Amazon CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/),
[CubeAPM — AWS CloudWatch pricing and review 2026](https://cubeapm.com/blog/aws-cloudwatch-pricing-and-review/)).
At 1,000 queries/day × ~10 spans × ~2 KB ≈ 0.6 GB/month ≈ **$0.21/month.**
Negligible.

### Recommendation

> **Start with direct OTLP to the X-Ray endpoint (6.1). Do not deploy an ADOT
> sidecar.** It is one environment variable and one IAM policy on a codebase
> that is already instrumented. Turn on **Transaction Search** to explore the
> `gen_ai.*` spans, and watch **CloudWatch GenAI Observability** as it leaves
> preview.
>
> Add an ADOT sidecar only when you need something the direct path cannot do:
> tail-based sampling, PII redaction in spans, or dual-shipping to a
> non-AWS backend.

**Flag: this project does not need a collector.** A sidecar is a real answer to
a problem this deployment does not have yet.

---

## 7. CI/CD: GitHub Actions → ECR → ECS

### 7.1 Authentication — OIDC, not access keys

Long-lived `AWS_ACCESS_KEY_ID` in GitHub Secrets is no longer defensible.
GitHub Actions issues a short-lived (~15 minute) OIDC token per workflow run;
AWS IAM trusts it via an OIDC provider at
`https://token.actions.githubusercontent.com` with audience `sts.amazonaws.com`,
and `aws-actions/configure-aws-credentials` exchanges it for temporary
credentials
([RKON — GitHub Actions on AWS: implementing identity federation](https://www.rkon.com/articles/github-actions-on-aws-how-to-implement-identity-federation/),
[CloudWebSchool — GitHub Actions for AWS: OIDC setup, ECS deployment](https://cloudwebschool.com/docs/aws/devops-and-cicd/github-actions-for-aws/)).

**The trust policy condition is the part people get wrong.** Without a `sub`
condition scoped to your repository *and branch*, any GitHub Actions workflow
anywhere can assume the role — the research is explicit that "people have been
pwned by this". Scope it:

```
"Condition": {
  "StringEquals": { "token.actions.githubusercontent.com:aud": "sts.amazonaws.com" },
  "StringLike":   { "token.actions.githubusercontent.com:sub": "repo:GojoV339/Multi_Agent_RAG_System_For_Enterprise_Log_And_Knowledge_Search:ref:refs/heads/main" }
}
```

Layer a **GitHub Environment with required reviewers** on the deploy job so a
human approves production pushes.

### 7.2 Image tagging

Do not deploy `:latest` — it is not a pointer to anything reproducible and it
defeats rollback. Tag every build with the **immutable Git SHA**, and
additionally move a `:latest` or `:prod` tag for human convenience only:

```
<acct>.dkr.ecr.<region>.amazonaws.com/aiops-copilot:sha-<short-sha>
```

Enable **ECR tag immutability** so a SHA tag can never be repointed, and an
**ECR lifecycle policy** to expire untagged images after ~7 days and keep the
last ~20 tagged ones — otherwise 2 GB images accumulate at $0.10/GB-month.

Register a new task-definition revision with the SHA tag and call
`aws ecs update-service --force-new-deployment`, or use
`aws-actions/amazon-ecs-deploy-task-definition`.

### 7.3 Safe rollout

ECS gained native deployment safety in 2025–2026 and it is now genuinely good:

- **Built-in blue/green deployments** (July 2025) — no CodeDeploy required. ECS
  provisions the new version alongside the old, lets you validate before shifting
  production traffic, then bakes for a configured period before finalising
  ([AWS — Amazon ECS enables built-in blue/green deployments](https://aws.amazon.com/about-aws/whats-new/2025/07/amazon-ecs-built-in-blue-green-deployments/),
  [AWS DevOps blog — Choosing between ECS Blue/Green Native or CodeDeploy in CDK](https://aws.amazon.com/blogs/devops/choosing-between-amazon-ecs-blue-green-native-or-aws-codedeploy-in-aws-cdk/)).
  Canary and linear strategies were added in Oct 2025, reaching parity with
  CodeDeploy.
- **Deployment circuit breaker** with automatic rollback, and since **July 2026**
  configurable thresholds — fixed failure count or percentage of desired count,
  consecutive or cumulative counting
  ([AWS — ECS now supports configurable deployment circuit breaker settings](https://aws.amazon.com/about-aws/whats-new/2026/07/amazon-ecs-circuit-breaker-settings/)).
- **1-click rollbacks** for service deployments (May 2025)
  ([AWS — ECS introduces 1-click rollbacks](https://aws.amazon.com/about-aws/whats-new/2025/05/amazon-ecs-1-click-rollbacks-service-deployments)).

**Recommended pipeline:**

1. `ruff` + `pytest -q` (already in `.github/workflows/ci.yml`)
2. `scripts/evaluate.py --gate` — **keep this as a hard deploy gate.** A retrieval
   or safety regression should block the image, not just the merge. This is the
   most distinctive thing in the pipeline and it belongs in CD, not only CI.
3. `docker buildx build --platform linux/arm64` → push to ECR with `sha-<sha>`
4. Generate the SOCI index for the image (SOCI is opt-in per image)
5. Register task-definition revision → `update-service` with **rolling update +
   circuit breaker enabled**, or **native blue/green** with a bake period
6. Smoke-test `/health` and `/metrics` against the ALB; rely on the circuit
   breaker for automatic rollback

Blue/green is the better answer for the API. For Streamlit, remember that a
deployment kills in-flight WebSocket sessions regardless of strategy — users get
a reconnect. That is acceptable for an internal console.

---

## 8. Reference architectures, and what they do that this project does not

### 8.1 AWS Prescriptive Guidance — "RAG options and architectures on AWS"

<https://docs.aws.amazon.com/prescriptive-guidance/latest/retrieval-augmented-generation-options/introduction.html>

AWS's own decision framework for choosing between fully managed services and
custom RAG architectures. Useful mainly as the canonical statement of the
option space (Bedrock Knowledge Bases / Kendra / OpenSearch / pgvector / custom).

**What it has that this project does not:** an explicit written justification of
*why* the custom path was chosen over the managed one. This project has the
argument (README, "No vector database") but has never framed it against AWS's
own decision tree. Doing so would strengthen it.

### 8.2 AWS Public Sector — "Well-rounded technical architecture for a RAG implementation on AWS"

<https://aws.amazon.com/blogs/publicsector/well-rounded-technical-architecture-for-a-rag-implementation-on-aws/>

A production RAG system ("Alfred") on GovCloud: S3 as the document lake, Lambda
for ingestion/preprocessing/text extraction, Amazon Transcribe for audio,
Amazon Kendra for retrieval and relevance ranking, DynamoDB for
millisecond-latency state, Bedrock (Claude) for generation, Bedrock Guardrails
for content filtering, CloudWatch for metrics/dashboards/alerts, IAM for access
control, KMS for encryption. FedRAMP High / DoD IL5 / ITAR compliant.

**What it has that this project does not:**
- **KMS encryption at rest** across every data store, declared explicitly.
- **Per-user / per-document access control on retrieval.** Kendra enforces
  document-level ACLs so a user never retrieves a chunk they cannot see. This
  project has no notion of a user at all, and for an SRE corpus containing
  post-mortems and incident data that is a real gap — a runbook for the payments
  service may be more sensitive than one for a docs site.
- **A managed ingestion pipeline** (event-driven on S3 PUT) rather than a script
  run by hand.
- **Multimodal ingestion** (Transcribe for audio/video).

**What this project has that it does not:** a measured ablation of every
retrieval stage, a calibrated confidence floor, deterministic claim
verification, and a CI quality gate. The AWS reference architecture describes
*components*; it does not describe how anyone knows the retrieval is good.

### 8.3 Community engineering write-up — "RAG Architecture on AWS: S3 + OpenSearch + Bedrock"

<https://devopsity.com/blog/rag-architecture-on-aws-s3-opensearch-bedrock-infrastructure-patterns-and-costs/>

The most useful of the three because it publishes numbers. Its "Small" tier —
10,000 documents, **1,000 queries/day**, the same traffic assumption used in this
document — costs approximately:

| Line | Small (10k docs, 1k queries/day) |
|---|---|
| S3 | £1 |
| OpenSearch Serverless | **£560** |
| Embeddings | £0.50 |
| Generation | £30 |
| Lambda | £2 |
| **Total** | **~£595/month** |

Its own conclusion: *"OpenSearch Serverless dominates at small/medium scale."*
It recommends 256–512 token chunks with 10–15% overlap, `ef_search=100`,
5–10 retrieved chunks max, and caching query embeddings in ElastiCache or
DynamoDB.

**This is the strongest single argument for the architecture recommended here.**
That £560/month OpenSearch line is a bill for a corpus 6× larger than this one,
and it is ~4× the *entire* infrastructure cost of the recommended design. (Note:
the figure assumes classic OpenSearch Serverless with its 2+2 OCU floor; the
NextGen scale-to-zero release of May 2026 cuts it substantially — but not to
zero, and it introduces cold starts.)

**What it has that this project does not:**
- **A query-embedding / retrieval-result cache.** For an SRE tool, incident
  questions cluster hard during an outage — the same three questions get asked
  by five people in ten minutes. A cache keyed on the normalised question would
  cut both the 2.4 s rerank and the Opus synthesis cost on repeats. This is the
  single highest-value idea from all three references.
- **An event-driven ingestion pipeline** (S3 PUT → Lambda → chunk → embed →
  index), rather than `scripts/setup.py`.

### Cross-cutting gaps

Common to all three references and absent here:

1. **Encryption at rest declared per data store** (KMS on S3, RDS, ECR).
2. **Authentication.** Every reference architecture has an identity layer. The
   Streamlit console currently has none — anyone who reaches the ALB gets the
   escalation queue and the audit trail. Minimum viable fix: **ALB
   authentication with Cognito or OIDC**, which requires no application code.
3. **Retrieval-time authorization.** Harder, and arguably out of scope for a
   portfolio project — but worth naming as a known limitation.
4. **A caching layer.**
5. **Event-driven ingestion.**

Also worth noting from the broader 2026 literature: the consensus is that
*"when RAG fails, the failure point is retrieval 73% of the time, not
generation"*, that hybrid search is *"the single biggest quality improvement for
naive RAG pipelines"*, and that reranking is *"the highest-ROI improvement"*
([Production-Ready RAG Architecture Patterns for 2026](https://www.agileinfoways.com/blog/building-production-ready-rag-systems-2026),
[NashTech — Building a production-ready RAG application](https://blog.nashtechglobal.com/building-a-production-ready-rag-application-architecture-challenges-and-lessons-learned/)).
This project independently measured all three of those conclusions and has the
ablation table to prove them. That is a stronger position than citing them.

---

## 9. Recommended architecture

```
                          Internet
                             │
                    ┌────────▼────────┐
                    │  Route 53 (opt) │
                    │  ACM certificate│
                    └────────┬────────┘
                             │ HTTPS 443
                  ┌──────────▼──────────┐
                  │        ALB          │  ← Cognito/OIDC auth (recommended)
                  │  host/path routing  │
                  └──┬───────────────┬──┘
        api.host ────┘               └──── ui.host
              │                             │
   ┌──────────▼──────────┐      ┌───────────▼─────────┐
   │  ECS Service: api   │      │ ECS Service: ui     │
   │  Fargate ARM64      │      │ Fargate ARM64       │
   │  2 vCPU / 4 GB      │      │ 1 vCPU / 2 GB       │
   │  FastAPI + uvicorn  │      │ Streamlit           │
   │  ONNX models baked  │      │ (calls api over ALB)│
   └───┬────────┬────────┘      └─────────────────────┘
       │        │
       │        └──── OTLP/HTTP (SigV4) ──► X-Ray OTLP endpoint
       │                                    └► CloudWatch Transaction Search
       │                                       + Application Signals
       │
   ┌───▼─────────────┐  ┌──────────────────┐  ┌───────────────────┐
   │ S3 (gateway     │  │ RDS PostgreSQL   │  │ Secrets Manager   │
   │  VPC endpoint)  │  │ db.t4g.micro     │  │  ANTHROPIC_API_KEY│
   │ vectors.npy     │  │ Single-AZ, 20 GB │  │  RDS credentials  │
   │ chunks.pkl      │  │ catalog / audit /│  ├───────────────────┤
   │ corpus source   │  │ escalations      │  │ SSM Param Store   │
   └─────────────────┘  └──────────────────┘  │  AIOPS_* config   │
                                              └───────────────────┘
                             │
                    api.anthropic.com  (synthesis; or Bedrock via PrivateLink)
```

**Deployment path:** GitHub Actions (OIDC) → build arm64 image → push to ECR
(`sha-<sha>`, immutable tags, lifecycle policy) → generate SOCI index → register
task definition → ECS blue/green with circuit-breaker rollback. The
`scripts/evaluate.py --gate` step blocks the deploy on a retrieval or safety
regression.

---

## 10. Bill of materials and monthly cost at 1,000 queries/day

### AWS infrastructure

| Service | Configuration | $/month |
|---|---|---|
| ECS Fargate — API | 1 task, 2 vCPU / 4 GB, ARM64, 730 h | **57.66** |
| ECS Fargate — UI | 1 task, 1 vCPU / 2 GB, ARM64, 730 h | **28.83** |
| Application Load Balancer | 1 ALB, ~1 LCU avg | **22.27** |
| RDS PostgreSQL | `db.t4g.micro`, Single-AZ, 20 GB gp3 | **13.98** |
| Public IPv4 | 2 task ENIs @ $0.005/h | **7.30** |
| Secrets Manager | 2 secrets @ $0.40 | **0.80** |
| SSM Parameter Store | standard params | **0.00** |
| ECR | ~6 GB stored @ $0.10/GB | **0.60** |
| S3 | 10 MB + requests; gateway endpoint free | **0.05** |
| CloudWatch Logs | ~1.5 GB ingest @ $0.50/GB | **0.75** |
| CloudWatch Transaction Search | ~0.6 GB spans @ $0.35/GB, 1% indexed free | **0.21** |
| Data transfer out | ~5 GB @ $0.09/GB | **0.45** |
| **AWS subtotal** | | **≈ $133/month** |

Fargate rates: $0.04048/vCPU-hour and $0.004445/GB-hour x86
([AWS Fargate pricing](https://aws.amazon.com/fargate/pricing/)), less 20% for
Graviton. ALB: $0.0225/hour base + $0.008/LCU-hour
([CloudZero — AWS ALB pricing 2026](https://www.cloudzero.com/blog/aws-alb-pricing/)).

**Variants:**
- Single-AZ HA (2 tasks per service across 2 AZs): **+$86/month → ~$219**
- NAT gateway instead of public task IPs: **+$29/month → ~$162**
- Full private with 5 interface endpoints × 2 AZs: **+$66/month → ~$199**
- EKS instead of ECS: **+$73/month**
- OpenSearch Serverless (classic) added: **+$700/month** — do not

### Anthropic API — the line that actually dominates

Model pricing (per million tokens): **Claude Opus 5 $5 in / $25 out**;
**Claude Haiku 4.5 $1 in / $5 out**. Cache reads ~0.1×, cache writes ~1.25×
(5-minute TTL). Opus 5's minimum cacheable prefix is 512 tokens.

Per query, using this project's routing (Haiku for triage + extraction, Opus for
synthesis with adaptive thinking):

| Call | Model | ~In | ~Out | $/query |
|---|---|---|---|---|
| Triage / routing | Haiku 4.5 | 1,500 | 200 | 0.0025 |
| Error-code extraction | Haiku 4.5 | 1,200 | 150 | 0.0020 |
| Synthesis | Opus 5 | 3,000 | 1,500 | 0.0525 |
| | | | **Total** | **≈ $0.057** |

**1,000 queries/day × 30 = ~$1,710/month.**

Three levers, in order of impact:

1. **Prompt-cache the system prompts.** They are stable across every request and
   comfortably over the 512-token Opus 5 minimum. Cache reads cost ~0.1× input,
   so this cuts the input side by roughly 90% — about **$45/month**. Modest,
   because output dominates.
2. **Route synthesis to Claude Sonnet 5** ($3/$15) where the golden set shows no
   quality loss: synthesis drops to ~$0.032/query → **~$1,060/month**. The
   evaluation harness is exactly the instrument for deciding whether this is
   safe, which is a good story to tell.
3. **Cache answers for repeated questions.** During an incident the same
   question arrives repeatedly. A normalised-question cache in RDS or ElastiCache
   would eliminate both the Opus call and the 2.4 s rerank on hits. This is the
   idea from §8.3 and it is the highest-leverage change available.

Also note the **retry loop**: `_retry_would_help` triggers a second synthesis on
weak answers. Budget for it — if 15% of queries retry, add ~8% to the synthesis
line.

### The headline

| | $/month |
|---|---|
| AWS infrastructure | **~$133** |
| Anthropic API (Opus 5 synthesis) | **~$1,710** |
| **Total** | **~$1,843** |

> **The model bill is roughly 13× the infrastructure bill.** Every hour spent
> optimising the AWS architecture below ~$133 is an hour not spent on the 93% of
> the cost that is token spend. Prompt caching, model routing, and answer caching
> are the three decisions that matter financially. The `/metrics` endpoint
> already reports actual `$/query` from span data — that is the instrument to
> optimise against, and having it is a genuine advantage.

---

## 11. Explicit "you do not need that" list

Stated plainly so it does not have to be re-litigated:

| Thing | Verdict | Why |
|---|---|---|
| **A vector database** | ❌ Not needed | 1,566 chunks = 2.4 MB. Managed options are built for 10M–2B vectors. Adopting one adds latency, cost, and a sync job to optimise something that is not the bottleneck. |
| **AWS App Runner** | ❌ Do not use | Maintenance mode from 30 Apr 2026; no new customers, no new features. |
| **EKS** | ❌ Not needed | +$73/month control plane for one Python service; ECS has none. |
| **Lambda** | ❌ Wrong shape | 250 MB of ONNX in the init path plus multi-second CPU inference; Streamlit is not a Lambda workload. |
| **Aurora Serverless v2** | ❌ Not needed | 5–6× more expensive than provisioned at steady low load; scale-to-zero adds a first-query latency spike. RDS `t4g.micro` is $14/month. |
| **DynamoDB** | ❌ Poor fit | The error catalog is a SQL join and the design argument depends on it being one. |
| **ADOT collector sidecar** | ❌ Not yet | Direct OTLP to the X-Ray endpoint is one env var on an already-instrumented codebase. Add the sidecar when you need tail sampling or PII redaction. |
| **Bedrock Knowledge Bases** | ❌ Not for this | Would delete `ingestion/`, `embedding/`, and `retrieval/` — the parts carrying the engineering argument. |
| **EFS for models** | ❌ Not needed | 250 MB baked into a 2 GB image is fine; EFS adds mount targets, NFS latency, and mutable state outside the image. |
| **VPC interface endpoints (5×)** | ❌ Not at this scale | ~$73/month, more than double a NAT gateway. Break-even is ~160 GB/month per service. |
| **Multi-AZ RDS** | ⚠️ Not yet | +$14/month for failover nobody is currently on call to use. |
| **GPU inference** | ⚠️ Later | Fargate has no GPU. If 2.4 s rerank becomes unacceptable, options are ECS on GPU EC2, SageMaker endpoints, or the Bedrock Rerank API ($1–2 per 1,000 queries). Note that ONNX int8 quantisation is **not** a reliable CPU win — reported cases show quantised models running *slower* than FP32 on CPU due to operator fallback ([microsoft/onnxruntime#12854](https://github.com/microsoft/onnxruntime/issues/12854)). Measure before assuming. |

---

## 12. First five things to build

1. **`Dockerfile`** — multi-stage, `python:3.11-slim` base, `uv pip install`,
   ONNX models baked in with `FASTEMBED_CACHE_PATH` pointed at them, `linux/arm64`.
2. **GitHub Actions OIDC role** with a `sub` condition scoped to
   `repo:GojoV339/...:ref:refs/heads/main`, and an ECR repository with immutable
   tags plus a lifecycle policy.
3. **Postgres migration** — port the three SQLite tables in
   `knowledge/catalog.py` behind the existing SQLAlchemy layer; keep SQLite as
   the local/CI backend so `pytest` needs no container.
4. **S3 index loading** — `retrieval/index.py` loads `vectors.npy` / `chunks.pkl`
   from a versioned S3 key at startup, falling back to the local path for dev.
5. **`AIOPS_OTLP_ENDPOINT` → X-Ray**, plus a task-role policy for
   `xray:PutSpans`. The `gen_ai.*` spans then show up in Transaction Search with
   no code change.

Everything after that is Terraform.
