# Sandbox image for the repo-triage agent.
#
# The image bakes in a *pristine clone* of the target repo with its test
# dependencies already installed. Each agent run gets a fresh container from
# this image, so "reset to pristine" is a container teardown, not a cleanup
# script -- there is no state to leak between issues.

FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates patch \
 && rm -rf /var/lib/apt/lists/*

# git apply / git grep work without an identity, but commits do not, and the
# agent may want to stash. Cheap insurance.
RUN git config --global user.email "agent@sandbox.local" \
 && git config --global user.name "Triage Agent" \
 && git config --global --add safe.directory '*'

ARG REPO_URL=https://github.com/asottile/pyupgrade
ARG REPO_REF=main

WORKDIR /work
RUN git clone "$REPO_URL" repo
WORKDIR /work/repo
RUN git checkout "$REPO_REF"

# Editable install so the agent's source edits take effect without reinstalling.
RUN pip install --no-cache-dir -e . \
 && pip install --no-cache-dir pytest

# Record the pristine ref inside the image so sandbox.py can reset to it
# without being told twice.
RUN git rev-parse HEAD > /work/PRISTINE_SHA

CMD ["sleep", "infinity"]
