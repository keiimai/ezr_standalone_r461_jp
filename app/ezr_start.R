local({
  root <- normalizePath(if (file.exists("R/project_ezr.R")) "." else "/app")
  ezr <- new.env()
  sys.source(file.path(root, "R", "project_ezr.R"), envir = ezr)
  ezr$ezr_start(root)
})
