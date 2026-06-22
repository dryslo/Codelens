"""Общие UI-компоненты: карточка результата поиска + панель флагов retrieval."""
import streamlit as st

from src.retrieval.flags import SearchFlags

_FLAG_LABELS = {"bm25": "BM25", "multiquery": "MultiQuery", "hyde": "HyDE",
                "rerank": "Rerank", "mmr": "MMR"}
_FLAG_HELP = {
    "bm25": "лексический канал, фьюзится с dense через RRF",
    "multiquery": "LLM генерит N переформулировок, каждая ищется отдельно",
    "hyde": "LLM генерит гипотетический код, добавляется к запросу",
    "rerank": "кросс-энкодер по топ-N кандидатам (тяжелее)",
    "mmr": "диверсификация финальной выдачи",
}
_LLM_DEPENDENT = {"multiquery", "hyde"}


def render_card(r: dict) -> None:
    """Рисует карточку результата поиска: метаданные и фрагмент кода."""
    m = r["meta"]
    pct = max(0, min(100, int(r.get("score", 0) * 100)))
    with st.container(border=True):
        head, badge = st.columns([5, 1], vertical_alignment="center")
        head.markdown(f"**`{m.get('file')}`** · {m.get('type')} `{m.get('name')}` · "
                      f"строки {m.get('start_line')}-{m.get('end_line')} · "
                      f"источник `{m.get('source')}`")
        badge.markdown(f"`{m.get('lang', '?')}` · **{pct}%**")
        st.code(r["code"], language=m.get("lang", "python"))


def flags_panel(policy: object, key_prefix: str, llms_available: bool,
                mode: str = "fast") -> SearchFlags:
    """Панель переключателей каналов. Скрывает policy=off, forced показывает плашкой."""
    ui_flags = policy.ui_visible()
    forced = policy.forced_for(mode)
    defaults = policy.defaults(mode=mode)
    st.markdown("**Каналы поиска**")

    values: dict[str, bool] = dict(forced)  # forced - каркас
    if ui_flags:
        cols = st.columns(len(ui_flags))
        for col, name in zip(cols, ui_flags):
            disabled = name in _LLM_DEPENDENT and not llms_available
            values[name] = col.toggle(
                _FLAG_LABELS[name],
                value=st.session_state.get(f"{key_prefix}_{name}", getattr(defaults, name)),
                key=f"{key_prefix}_{name}", disabled=disabled, help=_FLAG_HELP[name])
    else:
        st.caption("Все каналы зафиксированы политикой - UI-тумблеров нет.")

    forced_msgs = [f"{_FLAG_LABELS[n]}={'on' if v else 'off'} ({getattr(policy, n)})"
                   for n, v in forced.items()]
    if forced_msgs:
        st.caption("Принудительно политикой: " + " · ".join(forced_msgs))

    if (values.get("hyde") or values.get("multiquery")) and not llms_available:
        st.caption("HyDE/MultiQuery игнорируются: не настроен LLM-провайдер (см. config.yaml -> llm).")
    # Числовые параметры (k_cand/MMR λ/MultiQuery N) берутся из конфигурации (SearchFlags-дефолты),
    # в UI не настраиваются - оставлены только тумблеры каналов.
    return SearchFlags(
        bm25=values.get("bm25", False), multiquery=values.get("multiquery", False),
        hyde=values.get("hyde", False), rerank=values.get("rerank", False),
        mmr=values.get("mmr", False),
        k_cand=defaults.k_cand, mmr_lambda=defaults.mmr_lambda,
        multiquery_n=defaults.multiquery_n)
