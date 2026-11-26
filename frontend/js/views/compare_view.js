import { api } from "../api.js";
const { ref, onMounted } = Vue;
export const CompareView = {
  setup() {
    const flagId = ref(3); const envId = ref(2);
    const detail = ref(null); const matrix = ref(null); const err = ref(""); const msg = ref("");
    async function reload() {
      err.value = "";
      try {
        detail.value = await api(`/api/compare/flag/${flagId.value}/env/${envId.value}`);
        matrix.value = await api("/api/compare/matrix");
      } catch (e) { err.value = e.message || String(e); }
    }
    async function sweep() {
      err.value = ""; msg.value = "";
      try {
        const r = await api("/api/releases/expire-overdue", { method: "POST", body: "{}" });
        msg.value = `已同事务作废超龄影子 ${r.count} 条`;
        await reload();
      } catch (e) { err.value = e.message || String(e); }
    }
    onMounted(reload);
    return { flagId, envId, detail, matrix, err, msg, reload, sweep };
  },
  template: `
  <div class="grid">
    <div class="panel span-5">
      <h2>正式 vs 影子</h2>
      <p class="hint">超龄影子在对比读取时按甲路径同事务作废；已作废草稿不再算“齐套待升正”，只有 open 且未超龄的影子才计入 draft_ready。</p>
      <div class="field"><label>开关ID</label><input type="number" v-model.number="flagId" /></div>
      <div class="field"><label>环境ID</label><input type="number" v-model.number="envId" /></div>
      <button class="btn secondary" @click="reload">对比</button>
      <button class="btn danger" style="margin-left:.5rem" @click="sweep">作废全部超龄影子</button>
      <div class="err" v-if="err">{{ err }}</div>
      <p class="muted" v-if="msg">{{ msg }}</p>

      <div v-if="detail" style="margin-top:.8rem">
        <p>
          draft_ready（草稿齐套可升正）：
          <span :class="detail.draft_ready ? 'pill warn' : 'pill'">{{ detail.draft_ready ? '是' : '否' }}</span>
          <span class="pill danger" style="margin-left:.4rem" v-if="detail.void_release_shadows && detail.void_release_shadows.length">
            已作废 {{ detail.void_release_shadows.length }}
          </span>
        </p>
        <h3>未结发布影子（open）</h3>
        <table>
          <thead><tr><th>ID</th><th>类型</th><th>%</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="s in detail.open_release_shadows" :key="'or'+s.id">
              <td>{{ s.id }}</td><td>{{ s.kind }}</td><td>{{ s.percent }}</td>
              <td><span class="pill warn">{{ s.overdue ? '超龄' : 'open' }}</span></td>
            </tr>
            <tr v-if="!(detail.open_release_shadows || []).length"><td colspan="4" class="muted">无 open 发布影子</td></tr>
          </tbody>
        </table>
        <h3>已作废影子（不再齐套）</h3>
        <table>
          <thead><tr><th>ID</th><th>类型</th><th>%</th><th>状态/原因</th></tr></thead>
          <tbody>
            <tr v-for="s in detail.void_release_shadows" :key="'vr'+s.id">
              <td>{{ s.id }}</td><td>{{ s.kind }}</td><td>{{ s.percent }}</td>
              <td><span class="pill danger">{{ s.status }}</span> <span class="muted">{{ s.void_reason }}</span></td>
            </tr>
            <tr v-if="!(detail.void_release_shadows || []).length"><td colspan="4" class="muted">无作废记录</td></tr>
          </tbody>
        </table>
        <pre style="margin-top:0.8rem">{{ detail }}</pre>
      </div>
    </div>
    <div class="panel span-7">
      <h2>环境矩阵</h2>
      <div v-if="matrix">
        <table>
          <thead>
            <tr>
              <th>开关</th>
              <th v-for="e in matrix.envs" :key="e.id">{{ e.code }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in matrix.matrix" :key="row.flag_id">
              <td>{{ row.flag_key }}</td>
              <td v-for="c in row.cells" :key="c.env_id">
                <span v-if="c.has_rule">{{ c.percent }}% v{{ c.version }}</span>
                <span v-else class="muted">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>`
};
