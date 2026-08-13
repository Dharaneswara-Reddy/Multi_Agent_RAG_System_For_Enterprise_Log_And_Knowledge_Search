# ECS cluster, task definitions, services, autoscaling.

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = var.enable_container_insights ? "enhanced" : "disabled"
  }

  tags = local.tags
}

# Spot for the console only. The API is user-facing and a two-minute Spot
# interruption notice is not enough to drain a request that may still be
# running; the console is one internal user and can take the risk for ~70% off.
resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 0
  }
}

# --- task definitions -----------------------------------------------------

locals {
  # Secrets are injected by ARN and resolved by the ECS agent before the
  # container starts, so the values never appear in the task definition.
  # jsonencode of a Secrets Manager JSON blob supports `:key::` suffixes to
  # pull a single field, which is how the password comes out without the rest.
  common_secrets = [
    {
      name      = "AIOPS_DB_PASSWORD"
      valueFrom = "${aws_secretsmanager_secret.database.arn}:password::"
    },
    {
      name      = "ANTHROPIC_API_KEY"
      valueFrom = aws_secretsmanager_secret.anthropic.arn
    },
  ]

  environment_list = [for k, v in local.common_environment : { name = k, value = v }]
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
      essential = true
      command   = ["api"]

      portMappings = [
        {
          containerPort = var.api_container_port
          protocol      = "tcp"
        },
      ]

      environment = concat(
        local.environment_list,
        [{ name = "AIOPS_API_PORT", value = tostring(var.api_container_port) }],
      )
      secrets = local.common_secrets

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "api"
        }
      }

      # Container-level check as well as the ALB's. This one decides whether
      # ECS restarts the task; the ALB's decides whether it receives traffic.
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${var.api_container_port}${var.api_health_check_path}', timeout=4).status==200 else 1)\""]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 120 # loading the index plus two ONNX sessions
      }

      # Give uvicorn time to finish an in-flight answer before SIGKILL. The
      # entrypoint execs into it so the signal actually arrives.
      stopTimeout = 30
    },
  ])

  tags = local.tags
}

resource "aws_ecs_task_definition" "ui" {
  count = var.enable_ui ? 1 : 0

  family                   = "${local.name}-ui"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.ui_cpu
  memory                   = var.ui_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([
    {
      name      = "ui"
      image     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
      essential = true
      command   = ["ui"]

      portMappings = [
        {
          containerPort = var.ui_container_port
          protocol      = "tcp"
        },
      ]

      environment = concat(
        local.environment_list,
        [
          { name = "AIOPS_UI_PORT", value = tostring(var.ui_container_port) },
          # Streamlit needs to know it is served under a prefix or every
          # internal link 404s behind the ALB rule.
          { name = "STREAMLIT_SERVER_BASE_URL_PATH", value = trim(var.ui_base_url_path, "/") },
        ],
      )
      secrets = local.common_secrets

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "ui"
        }
      }

      stopTimeout = 30
    },
  ])

  tags = local.tags
}

# --- services -------------------------------------------------------------

resource "aws_ecs_service" "api" {
  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  enable_execute_command = var.enable_execute_command

  network_configuration {
    subnets = aws_subnet.private[*].id
    # No public IP: egress is via NAT or endpoints, and this avoids the
    # per-ENI IPv4 charge that now applies to every public address.
    assign_public_ip = false
    security_groups  = [aws_security_group.tasks.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = var.api_container_port
  }

  # Long enough to cover a cold start. Too short and ECS kills a task that was
  # still loading its index, then does it again, forever.
  health_check_grace_period_seconds = var.api_health_check_grace_period

  # Roll forward one task at a time with the old one still serving.
  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100

  # Without this, a deploy of a broken image leaves the service stuck with no
  # healthy tasks and requires a human to notice. With it, ECS puts the last
  # good task definition back on its own.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  wait_for_steady_state = true

  lifecycle {
    # CI deploys by registering a new task definition revision. Terraform must
    # not treat that as drift and roll the image back on the next apply.
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_lb_listener.http]

  tags = local.tags
}

resource "aws_ecs_service" "ui" {
  count = var.enable_ui ? 1 : 0

  name            = "${local.name}-ui"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.ui[0].arn
  desired_count   = var.ui_desired_count

  enable_execute_command = var.enable_execute_command

  dynamic "capacity_provider_strategy" {
    for_each = var.ui_use_fargate_spot ? [1] : []

    content {
      capacity_provider = "FARGATE_SPOT"
      weight            = 1
    }
  }

  launch_type = var.ui_use_fargate_spot ? null : "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    assign_public_ip = false
    security_groups  = [aws_security_group.tasks.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.ui[0].arn
    container_name   = "ui"
    container_port   = var.ui_container_port
  }

  health_check_grace_period_seconds = var.api_health_check_grace_period

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_lb_listener.http]

  tags = local.tags
}

# --- autoscaling ----------------------------------------------------------

# CPU, not request count: the bottleneck is cross-encoder reranking, which is
# CPU-bound and takes seconds. Request-count targets would scale on a proxy for
# the thing that actually saturates.
resource "aws_appautoscaling_target" "api" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.api_min_capacity
  max_capacity       = var.api_max_capacity
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${local.name}-api-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value = var.api_cpu_target

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }

    # Slow to scale in, quick to scale out. A new task pays a cold start before
    # it serves anything, so removing capacity early is expensive to undo.
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}
