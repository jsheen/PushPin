#!/usr/bin/env bash
# Deploy PushPin's push pipeline to Google Cloud, mixed: the scorer on GKE, the card server on
# Cloud Run, each notifier slot as a Cloud Run job started by Cloud Scheduler, data in Cloud SQL.
#
# DRY RUN BY DEFAULT: without --apply it only prints the commands, so nothing is created and
# nothing is billed. Read them first; add --apply to run them.
#
#   scripts/deploy-gcp.sh setup    [--apply]   # once: APIs, registry, accounts, Cloud SQL, secrets, cluster, KEDA, data
#   scripts/deploy-gcp.sh images   [--apply]   # build both images on Cloud Build, push to Artifact Registry
#   scripts/deploy-gcp.sh deploy   [--apply]   # cards (Cloud Run) -> scorer (GKE) -> notifier job -> schedule
#   scripts/deploy-gcp.sh pause    [--apply]   # stop what bills by the hour: scorer to 0, schedule paused, Cloud SQL off
#   scripts/deploy-gcp.sh resume   [--apply]   # undo pause
#   scripts/deploy-gcp.sh status               # what's running (read-only, always runs)
#   scripts/deploy-gcp.sh down     [--apply]   # delete everything this script created (keeps the bucket)
#
# Settings (environment variables): PROJECT (default: gcloud's), REGION (us-east1), BUCKET
# (pushpin-data), RUN (ablation-with-llm), TAG (git commit). NTFY_TOPIC is read from .env.
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
CLUSTER=pushpin
SQL=pushpin-db
SQL_INSTANCE=$PROJECT:$REGION:$SQL
RUN_SA=pushpin-run@$PROJECT.iam.gserviceaccount.com
KEDA_VERSION=2.17.0
export PROJECT REGION BUCKET RUN TAG SQL_INSTANCE

SECRETS=()   # values never printed (shown as ***)
run() {   # print a command; run it only with --apply
  local line="$*" s
  for s in ${SECRETS[@]+"${SECRETS[@]}"}; do line=${line//$s/***}; done
  printf '+ %s\n' "$line"
  if [[ $APPLY == 1 ]]; then eval "$@"; fi
}

render() {   # fill in ${...} placeholders in a deploy file
  envsubst '${PROJECT} ${REGION} ${BUCKET} ${RUN} ${TAG} ${SQL_INSTANCE} ${CARDS_URL} ${SCORER_URL}' < "$1"
}

lookup() {   # a value that only exists once the resource does: the real one with --apply
  if [[ $APPLY == 1 ]]; then eval "$1"; else echo "$2"; fi
}

cards_url() {
  lookup "gcloud run services describe pushpin-cards --region $REGION --format 'value(status.url)'" \
    "https://pushpin-cards-<hash>.$REGION.run.app"
}

scorer_url() {
  if [[ $APPLY == 1 ]]; then
    local ip=""
    for _ in $(seq 1 60); do   # the internal load balancer takes a minute or two to get its address
      ip=$(kubectl -n pushpin get svc scorer -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
      [[ -n $ip ]] && break
      sleep 10
    done
    [[ -n $ip ]] || { echo "the scorer's load balancer has no address yet" >&2; exit 1; }
    echo "http://$ip:8000"
  else
    echo "http://<scorer-internal-ip>:8000"
  fi
}

apply_file() {   # render a manifest and kubectl apply it
  printf '+ render %s | kubectl apply -f -\n' "$1"
  if [[ $APPLY == 1 ]]; then render "$1" | kubectl apply -f -; fi
}

case $STEP in
setup)
  [[ -n $PROJECT ]] || { echo "no project: gcloud config set project <id>" >&2; exit 1; }
  topic=$(grep -E '^NTFY_TOPIC=' .env 2>/dev/null | cut -d= -f2- || true)
  [[ -n $topic ]] || { echo "set NTFY_TOPIC in .env first" >&2; exit 1; }
  SECRETS+=("$topic")
  SECRETS+=("$topic")
  number=$(lookup "gcloud projects describe $PROJECT --format 'value(projectNumber)'" "<project-number>")

  echo "# APIs, image registry, the Cloud Run identity"
  run gcloud services enable run.googleapis.com container.googleapis.com sqladmin.googleapis.com \
    artifactregistry.googleapis.com cloudbuild.googleapis.com cloudscheduler.googleapis.com \
    secretmanager.googleapis.com
  run gcloud artifacts repositories create pushpin --repository-format=docker --location=$REGION
  run gcloud iam service-accounts create pushpin-run --display-name "'PushPin Cloud Run'"
  for role in roles/cloudsql.client roles/secretmanager.secretAccessor roles/run.invoker; do
    run gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$RUN_SA --role $role --condition=None
  done

  echo "# Cloud SQL: the smallest Postgres (~\$10/month while on; 'pause' turns it off)"
  password=$(openssl rand -hex 16); stats=$(openssl rand -hex 16)
  SECRETS+=("$password" "$stats")
  run gcloud sql instances create $SQL --database-version=POSTGRES_16 --edition=enterprise \
    --tier=db-f1-micro --region=$REGION --storage-size=10
  run gcloud sql databases create pushpin --instance=$SQL
  run gcloud sql users create pushpin --instance=$SQL --password=$password

  echo "# Secrets: the database URL (via the Cloud SQL socket), the ntfy topic, the /stats token"
  url="postgresql+psycopg://pushpin:$password@/pushpin?host=/cloudsql/$SQL_INSTANCE"
  run "printf %s '$url' | gcloud secrets create pushpin-database-url --data-file=-"
  run "printf %s '$topic' | gcloud secrets create pushpin-ntfy-topic --data-file=-"
  run "printf %s '$stats' | gcloud secrets create pushpin-stats-token --data-file=-"

  echo "# GKE Autopilot (the free tier covers one cluster's management fee) and KEDA"
  run gcloud container clusters create-auto $CLUSTER --region=$REGION
  run gcloud container clusters get-credentials $CLUSTER --region=$REGION
  run kubectl apply --server-side -f \
    https://github.com/kedacore/keda/releases/download/v$KEDA_VERSION/keda-$KEDA_VERSION.yaml

  echo "# The scorer's data and training run, in the bucket; the scorer may read it (Workload Identity)"
  run gcloud storage rsync data gs://$BUCKET/data --recursive --exclude "'nyt-recent.csv'"
  for f in config.yml metrics.json xgb_click.json xgb_return.json; do
    run gcloud storage cp outputs/$RUN/$f gs://$BUCKET/outputs/$RUN/$f
  done
  run gcloud storage buckets add-iam-policy-binding gs://$BUCKET --role roles/storage.objectViewer \
    --member "principal://iam.googleapis.com/projects/$number/locations/global/workloadIdentityPools/$PROJECT.svc.id.goog/subject/ns/pushpin/sa/scorer"
  ;;

images)
  run gcloud builds submit --config deploy/cloudbuild.yaml --substitutions=_REGION=$REGION,_TAG=$TAG .
  ;;

deploy)
  echo "# 1. Card server on Cloud Run, public (anyone with a push link can open its card)"
  printf '+ render deploy/cloudrun/cards.yaml | gcloud run services replace - --region %s\n' "$REGION"
  if [[ $APPLY == 1 ]]; then
    tmp=$(mktemp) && render deploy/cloudrun/cards.yaml > "$tmp" && gcloud run services replace "$tmp" --region $REGION
  fi
  run gcloud run services add-iam-policy-binding pushpin-cards --region $REGION --member allUsers --role roles/run.invoker
  CARDS_URL=$(cards_url); export CARDS_URL

  echo "# 2. Scorer on GKE, behind an internal load balancer, scaled by KEDA from the reader count"
  run gcloud container clusters get-credentials $CLUSTER --region=$REGION
  for f in namespace serviceaccount scorer-deployment scorer-service; do apply_file deploy/k8s/$f.yaml; done
  token=$(lookup "gcloud secrets versions access latest --secret pushpin-stats-token" "<stats-token>")
  SECRETS+=("$token")
  run "kubectl -n pushpin create secret generic cards-stats --from-literal=token=$token --dry-run=client -o yaml | kubectl apply -f -"
  apply_file deploy/k8s/scorer-scaledobject.yaml
  SCORER_URL=$(scorer_url); export SCORER_URL

  echo "# 3. Notifier slot as a Cloud Run job"
  printf '+ render deploy/cloudrun/notifier-job.yaml | gcloud run jobs replace - --region %s\n' "$REGION"
  if [[ $APPLY == 1 ]]; then
    tmp=$(mktemp) && render deploy/cloudrun/notifier-job.yaml > "$tmp" && gcloud run jobs replace "$tmp" --region $REGION
  fi

  echo "# 4. Cloud Scheduler: start the job at each slot hour, New York time"
  uri="https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/pushpin-notifier:run"
  verb=create
  [[ $APPLY == 1 ]] && gcloud scheduler jobs describe pushpin-slots --location $REGION >/dev/null 2>&1 && verb=update
  run gcloud scheduler jobs $verb http pushpin-slots --location $REGION --schedule "'0 8,12,17,21 * * *'" \
    --time-zone America/New_York --uri $uri --http-method POST --oauth-service-account-email $RUN_SA
  echo "# try a slot now: gcloud run jobs execute pushpin-notifier --region $REGION --wait"
  ;;

pause)
  run kubectl -n pushpin annotate scaledobject scorer autoscaling.keda.sh/paused-replicas=0 --overwrite
  run gcloud scheduler jobs pause pushpin-slots --location $REGION
  run gcloud sql instances patch $SQL --activation-policy NEVER
  ;;

resume)
  run gcloud sql instances patch $SQL --activation-policy ALWAYS
  run kubectl -n pushpin annotate scaledobject scorer autoscaling.keda.sh/paused-replicas-
  run gcloud scheduler jobs resume pushpin-slots --location $REGION
  ;;

status)
  APPLY=1
  echo "# project $PROJECT, region $REGION"
  run "gcloud run services list --region $REGION --filter metadata.labels.app=pushpin 2>/dev/null || true"
  run "gcloud run jobs list --region $REGION 2>/dev/null || true"
  run "gcloud scheduler jobs list --location $REGION 2>/dev/null || true"
  run "gcloud sql instances list 2>/dev/null || true"
  run "gcloud container clusters list 2>/dev/null || true"
  run "kubectl -n pushpin get deploy,svc,scaledobject 2>/dev/null || true"
  ;;

down)
  echo "# deletes the cluster, Cloud SQL (and its data), the Cloud Run service and job, the schedule,"
  echo "# the secrets and the image registry. The bucket is kept."
  run gcloud scheduler jobs delete pushpin-slots --location $REGION --quiet
  run gcloud run jobs delete pushpin-notifier --region $REGION --quiet
  run gcloud run services delete pushpin-cards --region $REGION --quiet
  run gcloud container clusters delete $CLUSTER --region $REGION --quiet
  run gcloud sql instances delete $SQL --quiet
  for s in pushpin-database-url pushpin-ntfy-topic pushpin-stats-token; do run gcloud secrets delete $s --quiet; done
  run gcloud artifacts repositories delete pushpin --location $REGION --quiet
  ;;

*)
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  ;;
esac

[[ $APPLY == 1 || $STEP == status || $STEP == help ]] || echo "# dry run: nothing was changed. Add --apply to run these."
