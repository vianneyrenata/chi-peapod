// Three read-only k6 scenarios for HFE Peapod. Nothing here writes, so production is a safe target.
//
//   k6 run -e SCENARIO=arrival   -e RATE=50   -e DURATION=60s k6_scenarios.js   # fixed 50 req/s "session break"
//   k6 run -e SCENARIO=soak      -e VUS=50    -e DURATION=30m k6_scenarios.js   # steady load, watch server memory
//   k6 run -e SCENARIO=firstload -e VUS=100   -e DURATION=60s k6_scenarios.js   # the 4 files a fresh browser downloads
//
// arrival: k6 keeps sending RATE requests/second even if answers get slow (a real crowd does that;
//   the other tests wait for each answer and so ease off). Step RATE up (50, 100, 200) to find the
//   ceiling. Read `dropped_iterations` in the output: nonzero means k6 could not keep the rate.
// soak: short bursts miss slow leaks (the 800 MB match-cache bug was one). While it runs, watch
//   `free -m` on the server every few minutes.
// firstload: attendees feel this download far more than any API call.
import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8000';
const SCENARIO = __ENV.SCENARIO || 'arrival';
const VUS = Number(__ENV.VUS || 50);
const DURATION = __ENV.DURATION || '60s';
const PERSON_IDS = JSON.parse(open('./embeddings_person_ids.json'));

const SCENARIOS = {
  arrival:   { executor: 'constant-arrival-rate', rate: Number(__ENV.RATE || 50), timeUnit: '1s',
               duration: DURATION, preAllocatedVUs: 100, maxVUs: 1000, exec: 'browse' },
  soak:      { executor: 'constant-vus', vus: VUS, duration: DURATION, exec: 'browse' },
  firstload: { executor: 'ramping-vus', exec: 'firstLoad',
               stages: [{ duration: '10s', target: VUS }, { duration: DURATION, target: VUS }] },
};

export const options = {
  scenarios: { [SCENARIO]: SCENARIOS[SCENARIO] },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    'http_req_duration{kind:api}': ['p(95)<500'],
    'http_req_duration{kind:asset}': ['p(95)<5000'],
  },
};

function randomPersonId() {
  return encodeURIComponent(PERSON_IDS[Math.floor(Math.random() * PERSON_IDS.length)]);
}

export function browse() {
  const roll = Math.random();
  const tags = { kind: 'api' };
  let res;
  if (roll < 0.7)      res = http.get(`${BASE_URL}/api/matches?personId=${randomPersonId()}&offset=0&limit=50`, { tags: { ...tags, name: 'matches' } });
  else if (roll < 0.9) res = http.get(`${BASE_URL}/api/placement?personId=${randomPersonId()}`, { tags: { ...tags, name: 'placement' } });
  else                 res = http.get(`${BASE_URL}/api/health`, { tags: { ...tags, name: 'health' } });
  check(res, { 'status is 200': (r) => r.status === 200 });
  if (SCENARIO !== 'arrival') sleep(1 + Math.random() * 4); // arrival paces itself; the others need think time
}

export function firstLoad() {
  // What loadRealData() in index.html fetches on a cold start, in parallel like a browser.
  const tags = { kind: 'asset' };
  const res = http.batch([
    ['GET', `${BASE_URL}/index.html`,            null, { tags: { ...tags, name: 'index.html' } }],
    ['GET', `${BASE_URL}/authors.csv`,  null, { tags: { ...tags, name: 'authors.csv' } }],
    ['GET', `${BASE_URL}/umap_positions.json`,   null, { tags: { ...tags, name: 'positions.json' } }],
    ['GET', `${BASE_URL}/program.csv`,  null, { tags: { ...tags, name: 'program.csv' } }],
  ]);
  check(res, { 'all 4 files 200': (rs) => rs.every((r) => r.status === 200) });
  sleep(5 + Math.random() * 5); // a person loads once, then reads for a while
}
