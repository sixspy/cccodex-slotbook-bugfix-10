export const holdsViewTemplate = `
  <div v-show="tab===1" class="panel">
    <h2>占位</h2>
    <div>
      <input v-model="holdForm.slot_code" placeholder="档期编码" />
      <input v-model="holdForm.party_key" placeholder="当事人" />
      <input v-model.number="holdForm.seats" type="number" min="1" style="width:5rem" />
      <button @click="doHold">占位</button>
    </div>
    <p class="err" v-if="actionErr">{{ actionErr }}</p>
    <p class="hint">
      排序：过期时刻升序（同刻按单号）· 方案：甲-读前同事务重排并物化序键
      <span v-if="holdsMeta.fresh === false" class="err">（本次未重排，滚回旧序）</span>
    </p>
    <p class="err" v-if="holdsMeta.contended && holdsMeta.contended.length">
      争用档期：{{ holdsMeta.contended.join('、') }}（后到只留争用痕，未改过期时刻）
    </p>
    <table>
      <thead><tr><th>序</th><th>ID</th><th>档</th><th>当事人</th><th>席</th><th>态</th><th>过期</th><th>续期</th></tr></thead>
      <!-- 只展示后端按过期时刻物化的 order_seq，禁止前端排序兜底 -->
      <tbody>
        <tr v-for="h in holds" :key="h.id">
          <td>{{ h.order_seq }}</td>
          <td>{{ h.id }}</td><td>{{ h.slot_code }}</td><td>{{ h.party_key }}</td>
          <td>{{ h.seats }}</td><td>{{ h.status }}</td><td>{{ h.expire_at }}</td>
          <td>
            <button class="ghost" v-if="h.status==='占位中'" @click="doRenew(h.id)">续期</button>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
`;
