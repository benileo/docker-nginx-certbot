#!/bin/bash

IMAGE=$(docker build -q ./ )
BOULDER_DIR=${1:-${BOULDER_DIR}}
ACME_SERVER="http://127.0.0.1:4000/directory"

function boulder_is_running {
    curl ${ACME_SERVER} --silent > /dev/null
}

function start_boulder {
    if boulder_is_running; then echo "boulder is running..."; return; fi

    if [ -z "${BOULDER_DIR}" ]; then
        echo "where is boulder..?";
        exit 1;
    fi

    cd ${BOULDER_DIR};
    set -e \
        && docker-compose down \
        && docker-compose run -d \
            --service-ports \
            -e FAKE_DNS=172.17.0.1 \
            boulder \
        && cd - > /dev/null
     while ! boulder_is_running ; do sleep 1; done
     echo "boulder is running..."
}

function killPort {
	lsof -i tcp:${1} -sTCP:listen | awk 'NR!=1 {print $2}' | xargs kill -9
}

redirect_pid=
function start_redirect {
    killPort 5002
    python redirect.py &
    redirect_pid=$!
}

temp_dir=$(mktemp -d)
echo "temp dir: ${temp_dir}"

function test_with_correct_env_vars {

    local container_id=$()
}

function cleanup {
    sleep 3  # no rush - do it right
    echo 'performing cleanup'
    if [ -n ${redirect_pid} ]; then kill -9 ${redirect_pid} 2> /dev/null; fi
    exit 0
}

while sudo netstat -lnt46p | grep -E '(433|5002)'; do
    echo 'killing already running servers'
    sudo netstat -lnt46p \
        | grep -E '(443|5002)' \
        | awk '{split($7, a, "/"); print a[1]}' \
        | sudo xargs kill -9
    sleep 1
done

start_boulder
start_redirect

trap cleanup SIGINT

docker run \
    --network=host \
    -e SERVER=http://127.0.0.1:4000/directory \
    -e DOMAIN=le1.wtf \
    -e EMAIL=test@example.com \
    -e DEBUG=1 \
    -v ${temp_dir}/letsencrypt:/etc/letsencrypt \
    ${IMAGE} &

container_pid=$!
{
    sleep 30
    kill -2 ${container_pid}
} &

wait ${container_pid}
echo "first test done"
echo
echo

docker run \
    --network=host \
    -e SERVER=http://127.0.0.1:4000/directory \
    -e DOMAIN=le1.wtf \
    -e EMAIL=test@example.com \
    -v ${temp_dir}/letsencrypt:/etc/letsencrypt \
    ${IMAGE} &

container_pid2=$!
{
    sleep 15
    kill -2 ${container_pid2}
} &

wait ${container_pid2}
echo "second test done"
echo
echo

cleanup