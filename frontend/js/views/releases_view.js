import { api } from "../api.js";
const { ref, onMounted } = Vue;
export const ReleasesView = {
  setup() {
    const official = ref([]); const shadows = ref([]);
    const voidCount = ref(0); const overdueCount = ref(0);
    const form = ref({
      flag_id: 3, env_id: 1, kind: "percent", percent: 30, cohort_id: null,
      summary: "开发环境三成灰度试跑", use_shadow: false,
    });
    const err = ref(""); const msg = ref(""); const duelMsg = ref(""); const busy = ref(false);
    async function reload() {
      const res = await api("/api/releases");
      official.value = res.official || []; shadows.value = res.shadows || [];
      voidCount.value = res.void_count || 0; overdueCount.value = res.overdue_count || 0;
    }
    async function publish() {
      err.value = ""; msg.value = "";
      try {
        const body = { ...form.value, cohort_id: form.value.cohort_id || null };
        const res = await api("/api/releases/publish", { method: "POST", body: JSON.stringify(body) });
        msg.value = `已提交：${res.mode}`; await reload();
      } catch (e) { err.value = e.message || String(e); }
    }
    async function promote(id) {
      err.value = "";
      try { await api(`/api/releases/shadow/${id}/promote`, { method: "POST", body: "{}" }); await reload(); }
      catch (e) { err.value = e.message || String(e); await reload(); }
    }
    async function voidShadow(id) {
      err.value = ""; msg.value = "";
      try {
        const r = await api(`/api/releases/shadow/${id}/void`, { method: "POST", body: "{}" });
        msg.value = r.already_void ? `#${id} 此前已作废` : `#${id} 已作废`;
        await reload();
      } catch (e) { err.value = e.message || String(e); await reload(); }
    }
    async function sweepOverdue() {
      err.value = ""; msg.value = ""; busy.value = true;
      try {
        const r = await api("/api/releases/expire-overdue", { method: "POST", body: "{}" });
        msg.value = `本次同事务作废超龄影子 ${r.count} 条：${(r.voided || []).join(", ") || "无"}`;
        await reload();
      } catch (e) { err.value = e.message || String(e); }
      finally { busy.value = false; }
    }
    // 并发双请求 duel_shadow_expire_void（前端同会话版，CAS 兜底，只许一次正式写）。
    async function duel(id) {
      duelMsg.value = `#${id} 双请求并发中…`; err.value = "";
      const calls = [
        api(`/api/releases/shadow/${id}/promote`, { method: "POST", body: "{}" })
          .then((r) => ({ ok: true, text: `胜方 200 v${r.version}` }))
          .catch((e) => ({ ok: false, text: `败方 ${e.message}` })),
        api(`/api/releases/shadow/${id}/promote`, { method: "POST", body: "{}" })
          .then((r) => ({ ok: true, text: `胜方 200 v${r.version}` }))
          .catch((e) => ({ ok: false, text: `败方 ${e.message}` })),
      ];
      const rs = await Promise.all(calls);
      const wins = rs.filter((x) => x.ok).length;
      duelMsg.value = ` duel_shadow_expire_void #${id}：胜方 ${wins} 个｜${rs.map((x) => x.text).join(" ／ ")}`;
      await reload();
    }
    function statusPill(s) {
      if (s.status === "open" && s.overdue) return { cls: "pill danger", text: "超龄·应作废" };
      if (s.voided || ["void", "voided", "expired", "discarded"].includes(s.status))
        return { cls: "pill danger", text: "已作废" };
      if (s.status === "promoted") return { cls: "pill", text: "已升正" };
      if (s.status === "open") return { cls: "pill warn", text: "开放中" };
      return { cls: "pill", text: s.status };
    }
    function ageText(s) {
      if (s.age_seconds == null) return "-";
      const h = Math.floor(s.age_seconds / 3600);
      if (h >= 24) return `${Math.floor(h / 24)}天${h % 24}时`;
      if (h >= 1) return `${h}时`;
      return `${Math.max(0, Math.floor(s.age_seconds / 60))}分`;
    }
    onMounted(reload);
    return {
      official, shadows, form, err, msg, duelMsg, busy, voidCount, overdueCount,
      publish, promote, voidShadow, sweepOverdue, duel, reload, statusPill, ageText,
    };
  },
  template: `
  <div class="grid">
    <div class="panel span-5">
      <h2>发布生效</h2>
      <p class="hint">正确性口径择一贯彻<strong>甲路径</strong>：超龄检测与作废同事务，升正原子写正式区；不设作废意图草案，不与乙路径混用。</p>
      <div class="field"><label>开关ID</label><input type="number" v-model.number="form.flag_id" /></div>
      <div class="field"><label>环境ID</label><input type="number" v-model.number="form.env_id" /></div>
      <div class="field"><label>类型</label>
        <select v-model="form.kind"><option>percent</option><option>cohort</option><option>percent_and_cohort</option></select>
      </div>
      <div class="field"><label>比例</label><input type="number" v-model.number="form.percent" /></div>
      <div class="field"><label>说明</label><textarea v-model="form.summary" rows="2"></textarea></div>
      <div class="field"><label><input type="checkbox" v-model="form.use_shadow" /> 使用影子草案（乙路径，升正前正式零改）</label></div>
      <button class="btn" @click="publish">提交发布</button>
      <p class="muted" v-if="msg">{{ msg }}</p>
      <div class="err" v-if="err">{{ err }}</div>
    </div>
    <div class="panel span-7">
      <h2>正式发布</h2>
      <table><thead><tr><th>开关</th><th>环境</th><th>版本</th><th>说明</th></tr></thead>
      <tbody>
        <tr v-for="r in official" :key="r.id">
          <td>{{ r.flag_key }}</td><td>{{ r.env_code }}</td><td>v{{ r.version }}</td><td>{{ r.summary }}</td>
        </tr>
      </tbody></table>
      <div style="display:flex;align-items:center;gap:.6rem;margin:1rem 0 .4rem">
        <h2 style="margin:0">影子发布生命周期</h2>
        <span class="pill danger" v-if="voidCount">已作废 {{ voidCount }}</span>
        <span class="pill warn" v-if="overdueCount">超龄未处理 {{ overdueCount }}</span>
        <span style="flex:1"></span>
        <button class="btn danger" :disabled="busy" @click="sweepOverdue">一键同事务作废超龄</button>
        <button class="btn secondary" @click="reload">刷新</button>
      </div>
      <p class="muted" v-if="duelMsg" style="margin:.2rem 0">{{ duelMsg }}</p>
      <table><thead><tr><th>ID</th><th>开关/环境</th><th>比例</th><th>开放时长</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="s in shadows" :key="'s'+s.id">
          <td>{{ s.id }}</td>
          <td>{{ s.flag_id }} / {{ s.env_id }}</td>
          <td>{{ s.percent }}%</td>
          <td>{{ ageText(s) }}</td>
          <td><span :class="statusPill(s).cls">{{ statusPill(s).text }}</span>
            <div class="muted" v-if="s.void_reason">{{ s.void_reason }}<template v-if="s.voided_by"> · {{ s.voided_by }}</template></div>
          </td>
          <td style="white-space:nowrap">
            <button class="btn secondary" v-if="s.status==='open' && !s.overdue" @click="promote(s.id)">升正</button>
            <button class="btn danger" v-if="s.status==='open'" @click="voidShadow(s.id)">作废</button>
            <button class="btn secondary" v-if="s.status==='open' && !s.overdue" @click="duel(s.id)">并发决斗×2</button>
          </td>
        </tr>
      </tbody></table>
      <p class="muted">超龄 open 影子在打开发布列表、升正、求值联查或对比时，按甲路径在同一事务内作废；升正/作废双会话决斗败方返回 409、正式区零增并写争用痕。</p>
    </div>
  </div>`
};
