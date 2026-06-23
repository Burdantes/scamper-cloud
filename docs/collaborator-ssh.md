# Collaborator SSH access

`scamperctl` can attach one collaborator's public SSH key to every VM in a run.
The access is instance-scoped: it does not modify project-wide SSH metadata and
disappears when the disposable VMs are deleted.

## Obtain the public key

The collaborator generates their own key pair and keeps the private key:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/scamper-collaboration
```

They send only `~/.ssh/scamper-collaboration.pub`. Never request, receive, copy,
or commit the private key.

Supported public-key types are Ed25519, RSA, ECDSA, and OpenSSH security-key
variants. The CLI rejects private-key files, malformed base64, `root` as the
username, and unsafe Linux usernames.

## Provision shared VMs

Add both SSH options to the normal dry-run command:

```bash
scamperctl provision \
  --profile lab \
  --run shared-validation \
  --zones one-per-region \
  --machine-type e2-small \
  --disk-size-gb 10 \
  --count-per-zone 1 \
  --max-vms 50 \
  --estimated-vm-hourly-usd 0.05 \
  --estimated-disk-gb-monthly-usd 0.05 \
  --max-runtime-hours 1 \
  --max-estimated-cost-usd 3 \
  --ssh-user collaborator \
  --ssh-public-key /path/to/collaborator.pub
```

The CLI writes a local run-state file in this format:

```text
collaborator:ssh-ed25519 AAAA... collaborator@laptop
```

The generated `gcloud compute instances create` commands pass that file as the
`ssh-keys` instance-metadata value. The JSON plan includes the username,
fingerprint, and local metadata path, but omits the key body. Review the plan,
then repeat it with `--apply`.

## Connect

After provisioning, give the collaborator each VM's external IP. They connect
using the matching private key:

```bash
ssh -i ~/.ssh/scamper-collaboration collaborator@VM_EXTERNAL_IP
```

TCP/22 must be allowed by the VPC firewall. Prefer a rule restricted to the
collaborator's public source address rather than `0.0.0.0/0`. Firewall lifecycle
is currently outside `scamperctl`; verify it separately before provisioning a
large run.

## Security boundary

- Treat the collaborator as having full control of each disposable VM. Compute
  Engine guest configuration commonly grants metadata-created SSH users sudo.
- Docker control is root-equivalent. Do not attach a broadly privileged service
  account to a collaborator-accessible VM.
- Keep experiment secrets outside the image and VM whenever possible.
- Use the cost guard and automatic deletion deadline so access and resources
  expire together.
- Remove an active collaborator immediately by destroying the run. Do not wait
  for the maximum runtime.

## OS Login incompatibility

Compute Engine ignores metadata-based SSH keys when OS Login is enabled. When a
collaborator key is requested, the dry run checks project metadata and refuses
to proceed if OS Login is enabled. In that case, grant the collaborator the
appropriate OS Login IAM role instead of disabling OS Login silently.
