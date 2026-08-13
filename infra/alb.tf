# Load balancer and routing.
#
# One ALB serves both the API and the console, split by path. Two would double
# the fixed ~$16/month and buy nothing at this size.

resource "aws_lb" "main" {
  name               = substr("${local.name}-alb", 0, 32)
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  # The reranker takes seconds per query and the synthesis call is slower
  # still, so the default 60s idle timeout would cut answers off mid-flight.
  idle_timeout = 120

  drop_invalid_header_fields = true
  enable_deletion_protection = local.is_production

  tags = local.tags
}

resource "aws_lb_target_group" "api" {
  name        = substr("${local.name}-api", 0, 32)
  port        = var.api_container_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip" # awsvpc networking gives each task its own ENI

  health_check {
    enabled  = true
    path     = var.api_health_check_path
    matcher  = "200"
    interval = 30
    timeout  = 10
    # Two consecutive passes before a task receives traffic. One is enough to
    # be fooled by a task that answers /health while still loading the index.
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # Long enough to finish an in-flight answer, short enough that a deploy is
  # not held open by one slow request.
  deregistration_delay = 30

  # The console is stateful per session; the API is not, but sharing the
  # setting keeps the two target groups comparable.
  stickiness {
    type            = "lb_cookie"
    enabled         = false
    cookie_duration = 86400
  }

  tags = local.tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_lb_target_group" "ui" {
  count = var.enable_ui ? 1 : 0

  name        = substr("${local.name}-ui", 0, 32)
  port        = var.ui_container_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    enabled             = true
    path                = local.ui_health_check_path
    matcher             = "200"
    interval            = 30
    timeout             = 10
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  deregistration_delay = 30

  # Streamlit keeps per-session state in the server process, so a user whose
  # requests land on a different task loses their session. Sticky sessions are
  # a workaround, not a fix; the fix is not running two UI tasks.
  stickiness {
    type            = "lb_cookie"
    enabled         = true
    cookie_duration = 86400
  }

  tags = local.tags

  lifecycle {
    create_before_destroy = true
  }
}

# --- listeners ------------------------------------------------------------

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  # With a certificate, redirect. Without one, serve directly — otherwise a
  # stack brought up without a domain has no reachable endpoint at all, which
  # makes the first deploy impossible to verify.
  dynamic "default_action" {
    for_each = var.acm_certificate_arn != "" ? [1] : []

    content {
      type = "redirect"

      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }

  dynamic "default_action" {
    for_each = var.acm_certificate_arn == "" ? [1] : []

    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.api.arn
    }
  }
}

resource "aws_lb_listener" "https" {
  count = var.acm_certificate_arn != "" ? 1 : 0

  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = var.ssl_policy
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# --- rules ----------------------------------------------------------------

# The console is routed by path prefix and, optionally, restricted by source
# address. It exposes the audit trail and the escalation queue, so leaving it
# open to the internet is a decision rather than a default.
resource "aws_lb_listener_rule" "ui" {
  count = var.enable_ui ? 1 : 0

  listener_arn = var.acm_certificate_arn != "" ? aws_lb_listener.https[0].arn : aws_lb_listener.http.arn
  priority     = 100

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.ui[0].arn
  }

  condition {
    path_pattern {
      values = ["/${trim(var.ui_base_url_path, "/")}", "/${trim(var.ui_base_url_path, "/")}/*"]
    }
  }

  dynamic "condition" {
    for_each = length(var.ui_allowed_cidrs) > 0 ? [1] : []

    content {
      source_ip {
        values = var.ui_allowed_cidrs
      }
    }
  }
}
