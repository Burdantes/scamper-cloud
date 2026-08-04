packer {
  required_plugins {
    googlecompute = {
      source  = "github.com/hashicorp/googlecompute"
      version = "~> 1.2"
    }
  }
}

variable "project_id" {
  type = string
}

variable "zone" {
  type    = string
  default = "us-central1-c"
}

variable "image_family" {
  type    = string
  default = "scamper-worker-debian12"
}

locals {
  timestamp = regex_replace(timestamp(), "[- TZ:]", "")
}

source "googlecompute" "scamper_worker" {
  project_id              = var.project_id
  zone                    = var.zone
  source_image_family     = "debian-12"
  source_image_project_id = ["debian-cloud"]
  ssh_username            = "packer"
  image_name              = "scamper-worker-${local.timestamp}"
  image_family            = var.image_family
  image_description       = "Scamper worker with stable measurement dependencies preinstalled"
  image_labels = {
    role = "scamper-worker"
  }
}

build {
  sources = ["source.googlecompute.scamper_worker"]

  provisioner "file" {
    source      = "images/gcp-worker/requirements.txt"
    destination = "/tmp/scamper-worker-requirements.txt"
  }

  provisioner "shell" {
    script = "images/gcp-worker/provision.sh"
  }
}
