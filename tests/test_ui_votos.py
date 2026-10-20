"""Interfaz votada por 4/4 skills de diseño: selección de cliente sin reconstruir la lista y papelera neutra."""
from types import SimpleNamespace as NS

import ui_estilo as ui


def _app(clientes):
    import main
    a = main.App.__new__(main.App)
    lector = NS(listar_clientes=lambda **kw: list(clientes), contar_clientes=lambda c: len(clientes))
    a.ctrl, a.p, a.cid, a.filtro, a.cartera = NS(lector=lector), ui.CLARO, 1, "", "NR"
    a._pagina_clientes = 0
    a.lista_clientes = NS(controls=[])
    return a


def test_cambiar_de_cliente_no_reconstruye_la_lista():
    clientes = [{"id": i, "nombre": f"N{i}", "apellidos": "A", "nif": f"X{i}"} for i in (1, 2, 3)]
    a = _app(clientes)
    a._refrescar_clientes()
    filas = list(a.lista_clientes.controls)
    a._marcar_cliente(1, 3)
    assert a.lista_clientes.controls == filas                     # mismas filas: solo cambian dos
    p = ui.CLARO
    assert a._filas_cli[3][0].bgcolor == p.relleno and a._filas_cli[3][1]["bgcolor"] == p.relleno
    assert a._filas_cli[1][0].bgcolor == "#00000000" and a._filas_cli[1][2]["bgcolor"] == p.hover


def test_papelera_solo_roja_al_pasar_el_raton():
    p = ui.CLARO
    b = ui.boton_icono(ui.ft.Icons.DELETE_OUTLINE, None, p, "Eliminar", p.texto2, peligro=True)
    icono = b.content
    assert icono.color == p.texto2
    b.on_hover(NS(data="true"))
    assert icono.color == p.rojo and b.bgcolor == p.rojo_suave
    b.on_hover(NS(data="false"))
    assert icono.color == p.texto2
