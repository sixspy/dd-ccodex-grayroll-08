import { api } from "../api.js";
const { ref, onMounted } = Vue;
export const EvaluateView = {
  setup() {
    const form = ref({ flag_key: "feed.ranking_v2", env_code: "staging", subject_key: "u-1001", shadow_id: null });
    const result = ref(null); const board = ref(null); const err = ref("");
    async function reloadBoard() { board.value = await api("/api/evaluate/board"); }
    async function run() {
      err.value = ""; result.value = null;
      try {
        const body = { ...form.value };
        if (body.shadow_id === "" || Number.isNaN(Number(body.shadow_id))) delete body.shadow_id;
        else body.shadow_id = Number(body.shadow_id);
        result.value = await api("/api/evaluate", { method: "POST", body: JSON.stringify(body) });
      }
      catch (e) { err.value = e.message || String(e); }
      reloadBoard();
    }
    onMounted(reloadBoard);
    return { form, result, board, err, run, reloadBoard };
  },
  template: `
  <div class="grid">
    <div class="panel span-5">
      <h2>在线求值</h2>
      <p class="hint">求值<strong>只认正式规则版本</strong>（official=1）。点名影子时联查作废标记与开放时长，超龄影子同事务作废并拒认，影子比例不参与求值。</p>
      <div class="field"><label>开关键</label><input v-model="form.flag_key" /></div>
      <div class="field"><label>环境</label>
        <select v-model="form.env_code"><option>dev</option><option>staging</option><option>prod</option></select>
      </div>
      <div class="field"><label>主体键</label><input v-model="form.subject_key" /></div>
      <div class="field"><label>联查影子ID（可选）</label><input type="number" v-model.number="form.shadow_id" placeholder="release_shadows.id" /></div>
      <button class="btn" @click="run">求值并联查影子</button>
      <div class="err" v-if="err">{{ err }}</div>

      <div v-if="result" style="margin-top:.8rem">
        <p>
          结论：
          <span :class="result.enabled ? 'pill warn' : 'pill'">{{ result.enabled ? '命中 ON' : '未命中 OFF' }}</span>
          <span class="pill" style="margin-left:.4rem">来源 {{ result.effective_source || 'official' }}</span>
          <span class="pill" style="margin-left:.4rem" v-if="result.version != null">正式 v{{ result.version }}</span>
        </p>
        <p v-if="result.shadow_id != null" style="margin:.4rem 0">
          <span class="pill danger" v-if="result.shadow_rejected">已拒认影子 #{{ result.shadow_id }}：{{ result.shadow_status }}</span>
          <span class="pill warn" v-else>影子 #{{ result.shadow_id }} 非生效来源（{{ result.shadow_status }}）</span>
        </p>
        <p class="muted" v-if="result.shadow_reason">联查原因：{{ result.shadow_reason }}</p>
        <pre style="margin-top:0.6rem">{{ result }}</pre>
      </div>
    </div>
    <div class="panel span-7">
      <h2>正式规则板</h2>
      <p class="muted" v-if="board">
        official_only={{ board.official_only }} · 开放影子 {{ board.open_shadow_count }} ·
        已作废影子 {{ board.void_shadow_count }} · 超龄未处理 {{ board.overdue_open_count }}
      </p>
      <table v-if="board">
        <thead><tr><th>开关</th><th>环境</th><th>类型</th><th>%</th><th>版本</th></tr></thead>
        <tbody>
          <tr v-for="(r,i) in board.rules" :key="i">
            <td>{{ r.flag_key }}</td><td>{{ r.env_code }}</td><td>{{ r.kind }}</td><td>{{ r.percent }}</td><td>v{{ r.version }}</td>
          </tr>
        </tbody>
      </table>
      <button class="btn secondary" style="margin-top:0.7rem" @click="reloadBoard">刷新</button>
    </div>
  </div>`
};
