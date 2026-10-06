// k6 load test for HFE Peapod, matching locustfile.py's traffic mix.
// Install once:  sudo snap install k6
// Run:           k6 run k6_test.js
// Against prod (read-only only!): k6 run -e BASE_URL=http://127.0.0.1:8000 k6_test.js
//
// WRITES=1 adds POST /api/profile — the request that queues the UMAP incremental placement
// (queue_rebuild() in server.py). NEVER run WRITES=1 against production: it upserts a real
// attendee's profile by Person ID and moves their dot. Only against a throwaway local copy
// (scratch data files + HFES_STATE_DIR/HFES_DATABASE_URL), same rule as loadtest.py/locustfile.py.
//   k6 run -e WRITES=1 k6_test.js
import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8000';
const WRITES = __ENV.WRITES === '1';
const WRITE_FRACTION = 0.1;
const PERSON_IDS = JSON.parse(open('./embeddings_person_ids.json'));

export const options = {
  stages: [
    { duration: '30s', target: 800 },  // burst ramp up to 800 virtual users
    { duration: '10s', target: 100 },  // quickly settle back down to 100
    { duration: '60s', target: 100 },  // hold flat at 100
    { duration: '10s', target: 0 },    // ramp down
  ],
  thresholds: {
    http_req_failed: ['rate<0.01'],     // fail the run if >1% of requests error
    http_req_duration: ['p(95)<500'],   // fail the run if p95 latency > 500ms
  },
};

function randomPersonId() {
  return encodeURIComponent(PERSON_IDS[Math.floor(Math.random() * PERSON_IDS.length)]);
}

export default function () {
  let res;
  if (WRITES && Math.random() < WRITE_FRACTION) {
    const pid = PERSON_IDS[Math.floor(Math.random() * PERSON_IDS.length)];
    res = http.post(`${BASE_URL}/api/profile`, JSON.stringify({
      profileId: pid, personId: pid, name: 'Load Test', aff: 'Load Test Co',
      bio: `load test bio ${Math.random()}`,
    }), { headers: { 'Content-Type': 'application/json' }, tags: { name: 'profile-write' } });
  } else {
    const roll = Math.random();
    if (roll < 0.7) {
      res = http.get(`${BASE_URL}/api/matches?personId=${randomPersonId()}&offset=0&limit=50`, { tags: { name: 'matches' } });
    } else if (roll < 0.9) {
      res = http.get(`${BASE_URL}/api/placement?personId=${randomPersonId()}`, { tags: { name: 'placement' } });
    } else {
      res = http.get(`${BASE_URL}/api/health`, { tags: { name: 'health' } });
    }
  }
  check(res, { 'status is 200': (r) => r.status === 200 });
  sleep(1 + Math.random() * 4); // think time, same as locustfile.py's between(1, 5)
}
