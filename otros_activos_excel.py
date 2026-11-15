"""Extensión autorizada en libros generados; las plantillas permanecen intactas."""
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.comments import Comment
from openpyxl.worksheet.datavalidation import DataValidation

import otros_activos as oa


def preparar(wb):
    ws = wb.create_sheet(oa.HOJA)
    for campo, letra, tipo in oa.CAMPOS:
        celda = ws[f"{letra}1"]
        celda.value = campo.replace("_", " ").capitalize()
        celda.font = Font(name="Calibri", bold=True, color="FFFFFF")
        celda.fill = PatternFill("solid", fgColor="244E70")
        celda.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[letra].width = 20 if tipo != "txt" else 26
    ws.row_dimensions[1].height = 34
    ws.freeze_panes = "I2"
    ws.sheet_view.zoomScale = 80
    for campo, opciones in (("tipo", oa.TIPOS), ("ubicacion", oa.UBICACIONES), ("estado", ("Activo", "Baja"))):
        letra = next(l for c, l, _ in oa.CAMPOS if c == campo)
        validacion = DataValidation(type="list", formula1='"' + ','.join(opciones) + '"')
        validacion.errorTitle = "Valor no válido"
        validacion.error = "Selecciona una opción de la lista."
        validacion.showErrorMessage = True
        ws.add_data_validation(validacion)
        validacion.add(f"{letra}2:{letra}10000")
    ws["I1"].comment = Comment("Saldo total de la cuenta a 31/12, antes de titularidad.", "Contabilidad a Medida")
    ws["J1"].comment = Comment("Media del 4T ajustada conforme al art. 12 LIP: excluir fondos retirados para bienes incluidos, cancelación de deudas e ingresos de préstamos. No duplicar activos.", "Contabilidad a Medida")
    ws["K1"].comment = Comment("Valor fiscal TOTAL de la posición: media 4T para cotizados; liquidativo a 31/12 para fondos. Nunca precio unitario. Requiere revisión del asesor.", "Contabilidad a Medida")
    ws["M1"].comment = Comment("Importe por unidad: cotizados, negociación media del 4T; fondos, liquidativo a 31/12; cripto, precio EUR a 31/12. No confundir con el total de K.", "Contabilidad a Medida")
    ws["AE1"].comment = Comment("Justificación del asesor: sujeción en España, convenio, valoración y exenciones; no inferidas de nacionalidad o domicilio del bróker.", "Contabilidad a Medida")


def completar(wb, db, cid, ejercicio):
    ws = wb[oa.HOJA]
    for r, activo in enumerate(db.iterar("otros_activos", cid), 2):
        valor = oa.valorar_seguro(activo)
        # Fórmulas sin funciones recientes para Excel 2016; blancos no equivalen a datos conocidos.
        cuenta = f'IF(COUNT(I{r}:J{r})>0,MAX(I{r}:J{r}),IF(K{r}="",0,K{r}))'
        cantidad = f'IF(ISNUMBER(L{r}),L{r},_xlfn.NUMBERVALUE(SUBSTITUTE(L{r},",","."),".",","))'
        unitario = f'IF(ISNUMBER(M{r}),M{r},_xlfn.NUMBERVALUE(SUBSTITUTE(M{r},",","."),".",","))'
        cripto = f'IF(K{r}<>"",K{r},IF(AND(L{r}<>"",M{r}<>""),{cantidad}*{unitario},0))'
        balance = (f'IF(AND(S{r}=1,O{r}<>""),O{r},'
                   f'IF(COUNT(N{r}:R{r})=5,MAX(N{r},O{r},MAX(0,AVERAGE(P{r}:R{r}))/0.2),IF(K{r}="",0,K{r})))')
        nominal = f'IF(N{r}="",IF(K{r}="",0,K{r}),N{r})'
        expresion = (f'IF(C{r}="CUENTA",{cuenta},IF(C{r}="CRIPTO",{cripto},'
                     f'IF(C{r}="NO_COTIZADO",{balance},IF(C{r}="RENTA_FIJA_NO_COTIZADA",{nominal},{cripto}))))')
        ws[f"AF{r}"] = f'=IF(A{r}="","",IF(OR(AC{r}="Baja",C{r}="RESUMEN_CARTERA"),0,ROUND({expresion},2)))'
        # AF se muestra redondeado; la titularidad usa el valor sin redondear,
        # igual que Python, para no redondear dos veces el importe atribuible.
        ws[f"AG{r}"] = f'=IF(A{r}="","",IF(OR(AC{r}="Baja",C{r}="RESUMEN_CARTERA"),0,ROUND(IF(V{r}="",{expresion},V{r})*G{r},2)))'
        ws[f"AH{r}"] = f'=IF(A{r}="","",IF(AND(H{r}="ESPANA",T{r}<>1),AG{r},0))'
        ws[f"AF{r}"].comment = Comment(valor['regla'] + "\n" + "\n".join(valor['avisos']), "Contabilidad a Medida")
        for _, letra, tipo in oa.CAMPOS:
            celda = ws[f"{letra}{r}"]
            celda.font = Font(name="Calibri", size=11, color="1D2939")
            if tipo in ("eur", "formula"):
                celda.number_format = '#,##0.00 "€"'
            elif tipo == "pct":
                celda.number_format = "0.00%"
            if r % 2 == 0:
                celda.fill = PatternFill("solid", fgColor="F3F6F9")
    fin = max(2, ws.max_row)
    ws.auto_filter.ref = f"A1:AH{fin}"
    resumen = wb['00_RESUMEN']
    inicio = resumen.max_row + 3
    resumen.cell(inicio, 1, "OTROS ACTIVOS · PATRIMONIO 714")
    resumen.cell(inicio, 1).font = Font(bold=True, color="244E70", size=13)
    resumen.cell(inicio + 1, 1, "Bruto en España (incluye exentos)")
    resumen.cell(inicio + 1, 2, f'=SUMIFS(\'{oa.HOJA}\'!AG2:AG10000,\'{oa.HOJA}\'!B2:B10000,E4,\'{oa.HOJA}\'!H2:H10000,"ESPANA")')
    resumen.cell(inicio + 2, 1, "Otros activos sujetos (sin exentos)")
    resumen.cell(inicio + 2, 2, f'=SUMIF(\'{oa.HOJA}\'!B2:B10000,E4,\'{oa.HOJA}\'!AH2:AH10000)')
    resumen.cell(inicio + 3, 1, "Estado de revisión")
    filas = db.listar('otros_activos', cid, ejercicio=ejercicio)
    avisos = [a for f in filas for a in oa.valorar_seguro(f)['avisos']]
    avisos.extend(a for lista in oa.conciliar_carteras(filas).values() for a in lista)
    modelo = db.get_por_clave('patrimonio_714', cid, f'714|{ejercicio}') or {}
    if 'provisional' in str(modelo.get('observaciones') or '').casefold() or modelo.get('revision_automatica'):
        avisos.append('Revisión del modelo pendiente')
    estado = "PROVISIONAL: revisar activos" if avisos else "Revisión al exportar; sincroniza tras editar para validar normativa y convenio"
    resumen.cell(inicio + 3, 2, f'=IF(E4={int(ejercicio)},"{estado}","Ejercicio cambiado: sincroniza con la app para validar activos")')
    for r in (inicio + 1, inicio + 2):
        resumen.cell(r, 2).number_format = '#,##0.00 "€"'
