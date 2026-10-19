/*
ISP policy probe for Surge Panel.
All requests are forced through the configured Surge policy. The panel shows
third-party IPPure classification when available; classification is informational
and is not proof of account access, residential service, or real conversation use.
*/

const DEFAULT_TITLE = "ISP";
const DEFAULT_ICON = "antenna.radiowaves.left.and.right";
const DEFAULT_ERR_ICON = "xmark.seal.fill";
const DEFAULT_COLOR = "#4CAF50";
const DEFAULT_ERR_COLOR = "#D65C51";

const args = parseArgs(typeof $argument === "undefined" ? "" : $argument);
const title = args.title || DEFAULT_TITLE;
const policy = args.policy || "";
const expectedIp = args["expected-ip"] || args.expected_ip || "";
const icon = args.icon || DEFAULT_ICON;
const iconerr = args.iconerr || DEFAULT_ERR_ICON;
const iconColor = args["icon-color"] || DEFAULT_COLOR;
const iconerrColor = args["iconerr-color"] || DEFAULT_ERR_COLOR;

function parseArgs(raw) {
  const result = {};
  String(raw || "").split("&").forEach(function(part) {
    if (!part) return;
    const index = part.indexOf("=");
    const key = safeDecode(index >= 0 ? part.slice(0, index) : part);
    const value = safeDecode(index >= 0 ? part.slice(index + 1) : "");
    if (key) result[key] = value;
  });
  return result;
}

function safeDecode(value) {
  try {
    return decodeURIComponent(String(value || "").replace(/\+/g, "%20"));
  } catch (e) {
    return String(value || "");
  }
}

function statusCode(response) {
  if (!response) return 0;
  return Number(response.status || response.statusCode || 0);
}

function validIpv4(value) {
  const ip = String(value || "").trim();
  if (!/^(?:\d{1,3}\.){3}\d{1,3}$/.test(ip)) return "";
  return ip.split(".").every(function(part) { return Number(part) >= 0 && Number(part) <= 255; }) ? ip : "";
}

function countryFlag(countryCode) {
  const cc = String(countryCode || "").trim().toUpperCase();
  if (!/^[A-Z]{2}$/.test(cc)) return "";
  return cc.split("").map(function(char) {
    return String.fromCodePoint(127397 + char.charCodeAt(0));
  }).join("") + " " + cc;
}

function fail(content, detail) {
  $done({
    title: title,
    content: detail ? content + "\n" + detail : content,
    icon: iconerr,
    "icon-color": iconerrColor
  });
}

function ok(content) {
  $done({
    title: title,
    content: content,
    icon: icon,
    "icon-color": iconColor
  });
}

function policyExists(name) {
  if (!name) return false;
  try {
    if (typeof $surge === "undefined" || typeof $surge.selectGroupDetails !== "function") return false;
    const details = $surge.selectGroupDetails() || {};
    const groups = details.groups || {};
    const decisions = details.decisions || {};
    return Object.prototype.hasOwnProperty.call(groups, name) || Object.prototype.hasOwnProperty.call(decisions, name);
  } catch (e) {
    return false;
  }
}

function get(url, callback) {
  $httpClient.get({ url: url, timeout: 8, policy: policy, "auto-cookie": false }, callback);
}

function riskText(score) {
  if (typeof score !== "number" || !isFinite(score) || score < 0 || score > 100) return "未知";
  const label = score > 70 ? "极高" : score > 40 ? "高" : score > 15 ? "中" : "低";
  return label + "（" + Math.round(score) + "/100）";
}

function typeText(info) {
  if (info && info.isTor === true) return "Tor IP";
  if (info && (info.isProxy === true || info.isVpn === true)) return "代理IP";
  if (info && info.isResidential === true) return "住宅 IP";
  if (info && info.isResidential === false) return "机房 IP";
  return "类型未知";
}

function compact(info) {
  const ip = validIpv4(info && info.ip);
  const flag = countryFlag(info && info.countryCode);
  if (!ip || !flag) return null;
  return {
    ip: ip,
    content: flag + " | " + typeText(info) + " | 风险：" + riskText(info.fraudScore) + "\n" + ip
  };
}

function expectedMismatch(ip) {
  return expectedIp && ip !== expectedIp;
}

function anthroThenFinish(ip, content) {
  get("https://api.anthropic.com/v1/models", function(error, response) {
    const status = statusCode(response);
    if (error || (status !== 401 && status !== 200)) {
      fail("Anthropic 连通性异常", ip);
      return;
    }
    if (expectedMismatch(ip)) {
      fail("出口 IP 与预期不一致", "实际 " + ip + " / 预期 " + expectedIp);
      return;
    }
    ok(content);
  });
}

function fallbackIp(reason) {
  get("https://ipv4.icanhazip.com", function(error, response, data) {
    const ip = validIpv4(data);
    if (error || statusCode(response) !== 200 || !ip) {
      fail(reason || "信息查询失败");
      return;
    }
    if (expectedMismatch(ip)) {
      fail("出口 IP 与预期不一致", "实际 " + ip + " / 预期 " + expectedIp);
      return;
    }
    fail(reason || "信息查询失败", ip);
  });
}

if (!policyExists(policy)) {
  fail(policy ? "策略不可用：" + policy : "未配置策略");
} else {
  get("https://my.ippure.com/v1/info", function(error, response, data) {
    if (error || statusCode(response) !== 200 || !data) {
      fallbackIp("信息查询失败");
      return;
    }
    try {
      const parsed = JSON.parse(data);
      const result = compact(parsed);
      if (!result) {
        fallbackIp("信息查询失败");
        return;
      }
      anthroThenFinish(result.ip, result.content);
    } catch (e) {
      fallbackIp("信息查询失败");
    }
  });
}
