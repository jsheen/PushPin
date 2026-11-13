#!/usr/bin/env bash
# Deploy PushPin's push pipeline to Google Cloud, all on Cloud Run: the scorer as a private service
# that scales to zero between slots, the card server as a public one, each notifier slot as a job
# started by Cloud Scheduler. Data lives in Neon Postgres (free tier; NEON_DATABASE_URL in .env).
# The GKE + KEDA setup for the scorer (deploy/k8s/) is the at-scale design; this script doesn't use it.
#
# DRY RUN BY DEFAULT: without --apply it only prints the commands, so nothing is created and
# nothing is billed. Read them first; add --apply to run them.
#
#   scripts/deploy-gcp.sh setup    [--apply]   # once: APIs, registry, account, secrets, data
#   scripts/deploy-gcp.sh images   [--apply]   # build both images on Cloud Build, push to Artifact Registry
#   scripts/deploy-gcp.sh deploy   [--apply]   # cards -> scorer -> notifier job -> schedule
#   scripts/deploy-gcp.sh pause    [--apply]   # stop the schedule (idle services already cost nothing)
#   scripts/deploy-gcp.sh resume   [--apply]   # undo pause
#   scripts/deploy-gcp.sh status               # what's running (read-only, always runs)
#   scripts/deploy-gcp.sh down     [--apply]   # delete everything this script created (keeps the bucket)
#
# Settings (environment variables): PROJECT (default: gcloud's), REGION (us-east1), BUCKET
# (pushpin-data), RUN (ablation-with-llm), TAG (git commit). NTFY_TOPIC and NEON_DATABASE_URL
# (Neon's connection string, with ?sslmode=require) are read from .env.
set -euo pipefail
cd "$(dirname "$0")/.."

STEP=${1:-help}
APPLY=0
[[ ${2:-} == --apply ]] && APPLY=1

PROJECT=${PROJECT:-$(gcloud config get-value project 2>/dev/null)}
REGION=${REGION:-us-east1}
BUCKET=${BUCKET:-pushpin-data}
RUN=${RUN:-ablation-with-llm}
TAG=${TAG:-$(git rev-parse --short HEAD)}
RUN_SA=pushpin-run@$PROJECT.iam.gserviceaccount.com
export PROJECT REGION BUCKET RUN TAG

SECRETS=()   # values never printed (shown as ***)
run() {   # print a command; run it only with --apply
  local line="$*" s
  for s in ${SECRETS[@]+"${SECRETS[@]}"}; do line=${line//$s/***}; done
  printf '+ %s\n' "$line"
  if [[ $APPLY == 1 ]]; then eval "$@"; fi
}

render() {   # fill in ${...} placeholders in a deploy file
  envsubst '${PROJECT} ${REGION} ${BUCKET} ${RUN} ${TAG} ${CARDS_URL} ${SCORER_URL}' < "$1"
}

lookup() {   # a value that only exists once the resource does: the real one with --apply
  if [[ $APPLY == 1 ]]; then eval "$1"; else echo "$2"; fi
}

cards_url() {
  lookup "gcloud run services describe pushpin-cards --region $REGION --format 'value(status.url)'" \
    "https://pushpin-cards-<hash>.$REGION.run.app"
}

scorer_url() {
  lookup "gcloud run services describe pushpin-scorer --region $REGION --format 'value(status.url)'" \
    "https://pushpin-scorer-<hash>.$REGION.run.app"
}

replace() {   # render a Cloud Run file and `gcloud run <services|jobs> replace` it
  printf '+ render %s | gcloud run %s replace - --region %s\n' "$2" "$1" "$REGION"
  if [[ $APPLY == 1 ]]; then
    local tmp; tmp=$(mktemp) && render "$2" > "$tmp" && gcloud run "$1" replace "$tmp" --region $REGION
  fi
}

case $STEP in
setup)
  [[ -n $PROJECT ]] || { echo "no project: gcloud config set project <id>" >&2; exit 1; }
  topic=$(grep -E '^NTFY_TOPIC=' .env 2>/dev/null | cut -d= -f2- || true)
  [[ -n $topic ]] || { echo "set NTFY_TOPIC in .env first" >&2; exit 1; }
  db=$(grep -E '^NEON_DATABASE_URL=' .env 2>/dev/null | cut -d= -f2- || true)
  [[ -n $db ]] || { echo "set NEON_DATABASE_URL in .env first (Neon dashboard -> Connect; postgresql+psycopg://...?sslmode=require)" >&2; exit 1; }
  stats=$(openssl rand -hex 16)
  SECRETS+=("$topic" "$db" "$stats")

  echo "# APIs, image registry, the Cloud Run identity (may read secrets and the bucket, call the scorer)"
  run gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
    cloudscheduler.googleapis.com secretmanager.googleapis.com
  run gcloud artifacts repositories create pushpin --repository-format=docker --location=$REGION
  run gcloud iam service-accounts create pushpin-run --display-name "'PushPin Cloud Run'"
  for role in roles/secretmanager.secretAccessor roles/run.invoker; do
    run gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$RUN_SA --role $role --condition=None
  done
  run gcloud storage buckets add-iam-policy-binding gs://$BUCKET --role roles/storage.objectViewer \
    --member serviceAccount:$RUN_SA

  echo "# Secrets: the Neon database URL, the ntfy topic, the /stats token"
  run "printf %s '$db' | gcloud secrets create pushpin-database-url --data-file=-"
  run "printf %s '$topic' | gcloud secrets create pushpin-ntfy-topic --data-file=-"
  run "printf %s '$stats' | gcloud secrets create pushpin-stats-token --data-file=-"

  echo "# The scorer's data and training run, in the bucket (mounted read-only by the scorer)"
  run gcloud storage rsync data gs://$BUCKET/data --recursive --exclude "'nyt-recent.csv'"
  for f in config.yml metrics.json xgb_click.json xgb_return.json; do
    run gcloud storage cp outputs/$RUN/$f gs://$BUCKET/outputs/$RUN/$f
  done
  ;;

images)
  run gcloud builds submit --config deploy/cloudbuild.yaml --substitutions=_REGION=$REGION,_TAG=$TAG .
  ;;

deploy)
  echo "# 1. Card server, public (anyone with a push link can open its card)"
  replace services deploy/cloudrun/cards.yaml
  run gcloud run services add-iam-policy-binding pushpin-cards --region $REGION --member allUsers --role roles/run.invoker
  CARDS_URL=$(cards_url); export CARDS_URL

  echo "# 2. Scorer, private: only the pushpin-run account may call it; scales to zero between slots"
  replace services deploy/cloudrun/scorer.yaml
  SCORER_URL=$(scorer_url); export SCORER_URL

  echo "# 3. Notifier slot as a Cloud Run job"
  replace jobs deploy/cloudrun/notifier-job.yaml

  echo "# 4. Cloud Scheduler: start the job at each slot hour, New York time"
  uri="https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/pushpin-notifier:run"
  verb=create
  [[ $APPLY == 1 ]] && gcloud scheduler jobs describe pushpin-slots --location $REGION >/dev/null 2>&1 && verb=update
  run gcloud scheduler jobs $verb http pushpin-slots --location $REGION --schedule "'0 8,12,17,21 * * *'" \
    --time-zone America/New_York --uri $uri --http-method POST --oauth-service-account-email $RUN_SA
  echo "# try a slot now: gcloud run jobs execute pushpin-notifier --region $REGION --wait"
  ;;

pause)
  run gcloud scheduler jobs pause pushpin-slots --location $REGION
  ;;

resume)
  run gcloud scheduler jobs resume pushpin-slots --location $REGION
  ;;

status)
  APPLY=1
  echo "# project $PROJECT, region $REGION"
  run "gcloud run services list --region $REGION --filter metadata.labels.app=pushpin 2>/dev/null || true"
  run "gcloud run jobs list --region $REGION 2>/dev/null || true"
  run "gcloud scheduler jobs list --location $REGION 2>/dev/null || true"
  ;;

down)
  echo "# deletes the Cloud Run services and job, the schedule, the secrets and the image registry."
  echo "# The bucket and the Neon database (and its data) are kept."
  run gcloud scheduler jobs delete pushpin-slots --location $REGION --quiet
  run gcloud run jobs delete pushpin-notifier --region $REGION --quiet
  run gcloud run services delete pushpin-cards --region $REGION --quiet
  run gcloud run services delete pushpin-scorer --region $REGION --quiet
  for s in pushpin-database-url pushpin-ntfy-topic pushpin-stats-token; do run gcloud secrets delete $s --quiet; done
  run gcloud artifacts repositories delete pushpin --location $REGION --quiet
  ;;

*)
  sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
  ;;
esac

[[ $APPLY == 1 || $STEP == status || $STEP == help ]] || echo "# dry run: nothing was changed. Add --apply to run these."
