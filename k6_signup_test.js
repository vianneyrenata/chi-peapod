// Tests exactly 50 concurrent brand-new attendee signups arriving at once (a registration-desk
// rush), not mixed traffic. No profileId/personId is sent, matching a real new signup (index.html's
// confirmProfile() for someone with no existing match) -- server.py generates a fresh profile ID
// and queues an incremental placement. The real question this answers: do 50 simultaneous new
// signups coalesce into ONE embedding job like the design intends, or does each one queue its own?
//
// NEVER run against production -- only a throwaway copy (see k6_test.js's header for why).
//   k6 run -e BASE_URL=http://127.0.0.1:8795 k6_signup_test.js
import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8000';

export const options = {
  scenarios: {
    signup_rush: {
      executor: 'per-vu-iterations',
      vus: Number(__ENV.VUS || 50),
      iterations: 1,       // each of the 50 virtual users signs up exactly once
      maxDuration: '30s',
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
  },
};

export default function () {
  const uid = `${__VU}-${Date.now()}-${Math.random()}`;
  const res = http.post(`${BASE_URL}/api/profile`, JSON.stringify({
    name: `New Signup ${uid}`,
    aff: 'Load Test Conference',
    bio: `Brand new attendee bio ${uid}`,
  }), { headers: { 'Content-Type': 'application/json' } });
  check(res, { 'status is 200': (r) => r.status === 200 });
}
