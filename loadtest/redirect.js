// k6 load test: ~95% redirects, ~5% link creation.
//   k6 run -e BASE_URL=http://localhost:8080 -e PROFILE=short loadtest/redirect.js   (rehearsal)
//   k6 run -e BASE_URL=http://<alb-dns-name> --summary-export loadtest/results/run.json loadtest/redirect.js
import http from "k6/http";
import { check } from "k6";

const BASE_URL = (__ENV.BASE_URL || "http://localhost:8080").replace(/\/$/, "");
const POOL_SIZE = 200;

// Full run (~8 min): warm-up 10 users, then 50, 100, 200 users, ramp down.
const FULL = [
  { duration: "1m", target: 10 },
  { duration: "2m", target: 50 },
  { duration: "2m", target: 100 },
  { duration: "2m", target: 200 },
  { duration: "1m", target: 0 },
];
// Short run for a local rehearsal (~1 min).
const SHORT = [
  { duration: "10s", target: 10 },
  { duration: "20s", target: 40 },
  { duration: "10s", target: 0 },
];

export const options = {
  stages: __ENV.PROFILE === "short" ? SHORT : FULL,
  thresholds: {
    "http_req_duration{name:redirect}": ["p(95)<300"],
    http_req_failed: ["rate<0.01"],
  },
};

// 302 and 201 are the expected answers; anything else counts as a failed request.
http.setResponseCallback(http.expectedStatuses(302, 201));

// Create a pool of links once, so reads hit real rows.
export function setup() {
  const codes = [];
  for (let i = 0; i < POOL_SIZE; i++) {
    const res = http.post(
      `${BASE_URL}/api/links`,
      JSON.stringify({ url: `https://example.com/loadtest/${Date.now()}-${i}` }),
      { headers: { "Content-Type": "application/json" }, tags: { name: "setup" } },
    );
    if (res.status === 201) codes.push(res.json("short_code"));
  }
  if (codes.length < POOL_SIZE / 2) throw new Error("could not create the link pool; is the app up?");
  return { codes };
}

export default function (data) {
  if (Math.random() < 0.95) {
    const code = data.codes[Math.floor(Math.random() * data.codes.length)];
    // redirects: 0 -> do not follow the Location to other sites
    const res = http.get(`${BASE_URL}/${code}`, { redirects: 0, tags: { name: "redirect" } });
    check(res, { "redirect is 302": (r) => r.status === 302 });
  } else {
    const res = http.post(
      `${BASE_URL}/api/links`,
      JSON.stringify({ url: `https://example.com/loadtest/new/${Date.now()}-${Math.random()}` }),
      { headers: { "Content-Type": "application/json" }, tags: { name: "create" } },
    );
    check(res, { "create is 201": (r) => r.status === 201 });
  }
}
