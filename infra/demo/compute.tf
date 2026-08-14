# Fargate rather than ECS-on-EC2, because the AWS Free Plan restricts EC2 to
# six instance types and the largest ARM one is 2 GiB — which the host OS, the
# container runtime and the ECS agent have to share with a 1563 MB application.
#
# Fargate's memory limit applies to the container alone: nothing else is
# charged against it. That is what makes 2 GB sufficient here when 2 GiB of
# EC2 was not.

resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  force_delete = true
  tags         = { Name = local.name }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images quickly"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the last 3 tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["sha-"]
          countType     = "imageCountMoreThan"
          countNumber   = 3
        }
        action = { type = "expire" }
      },
    ]
  })
}

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name = "containerInsights"
    # Billed per metric. The log group carries what a demo needs.
    value = "disabled"
  }

  tags = { Name = local.name }
}

resource "aws_ecs_task_definition" "app" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    # Graviton: ~20% cheaper per vCPU-hour and onnxruntime ships aarch64
    # wheels. The image must be built --platform linux/arm64 to match, or the
    # task crash-loops on "exec format error".
    cpu_architecture = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "app"
      image     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
      essential = true
      command   = ["ui"]

      portMappings = [
        {
          containerPort = var.container_port
          protocol      = "tcp"
        },
      ]

      environment = local.container_environment

      # Injected by the execution role at start, never in `environment` —
      # which is readable by anyone holding ecs:DescribeTaskDefinition.
      secrets = [
        { name = "AIOPS_DB_PASSWORD", valueFrom = aws_ssm_parameter.db_password.arn },
        { name = "ANTHROPIC_API_KEY", valueFrom = aws_ssm_parameter.anthropic_api_key.arn },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "app"
        }
      }

      # Python, not curl. The runtime stage deliberately ships without curl, a
      # build toolchain or uv, so a curl-based check fails on every probe and
      # ECS kills the task after the start period — with "Task failed container
      # health checks" and exit code 0, which points at the application rather
      # than at the missing binary. The Dockerfile's own HEALTHCHECK already
      # used Python; this brings the task definition in line.
      #
      # "CMD" with an argv array rather than "CMD-SHELL": no shell means no
      # quoting to get wrong inside jsonencode.
      healthCheck = {
        command = [
          "CMD", "python", "-c",
          "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${var.container_port}/_stcore/health', timeout=4).status == 200 else 1)",
        ]
        interval = 30
        timeout  = 5
        retries  = 3
        # Generous: the corpus and index come down from S3 and both ONNX models
        # load before Streamlit answers anything.
        startPeriod = 240
      }
    },
  ])

  tags = { Name = local.name }
}

resource "aws_ecs_service" "app" {
  name            = local.name
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets = [aws_subnet.public[0].id]
    # Required, and it is what replaces a $32.85/month NAT gateway: without a
    # routable address the task cannot pull its image from ECR. Inbound is
    # still closed to everything but the load balancer's security group.
    assign_public_ip = true
    security_groups  = [aws_security_group.tasks.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = var.container_port
  }

  health_check_grace_period_seconds = 300

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # One task, so a rolling deploy has nowhere to put the replacement. 100/200
  # would wait for capacity that never arrives; this stops the old task first
  # and accepts ~2 minutes of downtime per deployment.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_lb_listener.http]

  tags = { Name = local.name }
}
