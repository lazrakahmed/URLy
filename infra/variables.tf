variable "region" {
  type    = string
  default = "eu-north-1"
}

variable "image_tag" {
  type        = string
  description = "Image tag (the git SHA) to deploy for both api and ui"
}

variable "db_multi_az" {
  type        = bool
  default     = false
  description = "Standby database in a second AZ (automatic failover). false while iterating, true for the final deploy"
}

variable "min_tasks" {
  type        = number
  default     = 2
  description = "Tasks per service (one per AZ)"
}

variable "alarm_email" {
  type        = string
  default     = "lazrak.edu@gmail.com"
  description = "Where alarm emails go (SNS subscription; confirm it once by clicking the link)"
}

variable "max_tasks" {
  type        = number
  default     = 6
  description = "Autoscaling ceiling for the API. 6 tasks x 10 DB connections = 60, below the ~100 the db.t4g.micro allows"
}
