resource "aws_db_subnet_group" "main" {
  name       = "urly"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "main" {
  identifier     = "urly"
  engine         = "postgres"
  engine_version = "17"
  instance_class = "db.t4g.micro"

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "urly"
  username = "urly"
  # RDS creates the master password and keeps it in Secrets Manager. It never passes through
  # Terraform variables, the repo or the images (it is read at runtime by ECS).
  manage_master_user_password = true

  multi_az               = var.db_multi_az
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false

  backup_retention_period = 1
  skip_final_snapshot     = true # demo environment: destroy must not leave snapshots behind
  deletion_protection     = false
  apply_immediately       = true
}
