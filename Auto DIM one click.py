import clr
import math

clr.AddReference("RevitAPI")
from Autodesk.Revit.DB import *

clr.AddReference("RevitServices")
from RevitServices.Persistence import DocumentManager
from RevitServices.Transactions import TransactionManager

doc = DocumentManager.Instance.CurrentDBDocument
view = doc.ActiveView

# ------------------------------------------------------------
# Inputs & Execution Gate
# ------------------------------------------------------------
manual_elements = IN[0] if len(IN) > 0 and IN[0] is not None else []
run_view_wide = IN[1] if len(IN) > 1 and IN[1] is not None else False
offset_mm = IN[2] if len(IN) > 2 and IN[2] is not None else 500
mode_input = IN[3] if len(IN) > 3 and IN[3] is not None else "Exterior"
run_script = IN[5] if len(IN) > 5 and IN[5] is not None else False

if not run_script:
    OUT = {"status": "Paused", "created": 0, "message": "Set the Run toggle to True."}
else:
    dim_type = None
    if len(IN) > 4 and IN[4] is not None:
        try:
            dt = UnwrapElement(IN[4])
            if isinstance(dt, DimensionType): dim_type = dt
        except: pass

    mode = "interior" if "interior" in str(mode_input).strip().lower() else "exterior"
    offset_ft = offset_mm / 304.8

    if view is None:
        OUT = {"status": "Invalid view", "created": 0}
    else:
        walls = []
        if run_view_wide:
            for w in FilteredElementCollector(doc, view.Id).OfClass(Wall):
                if w.Location is None: continue
                try:
                    is_ext = (w.WallType.Function == WallFunction.Exterior) or (w.WallType.Kind == WallKind.Curtain)
                    if mode == "exterior" and not is_ext: continue
                    if mode == "interior" and is_ext: continue
                    walls.append(w)
                except: pass
        else:
            if not isinstance(manual_elements, list): manual_elements = [manual_elements]
            for item in manual_elements:
                try:
                    el = UnwrapElement(item)
                    if isinstance(el, Wall) and el.Location is not None: walls.append(el)
                except: pass

        if not walls:
            OUT = {"status": "No valid walls found", "created": 0}
        else:
            def get_collinear_key(w):
                c = w.Location.Curve
                p1, p2 = c.GetEndPoint(0), c.GetEndPoint(1)
                dx, dy = p2.X - p1.X, p2.Y - p1.Y
                angle = math.atan2(dy, dx)
                if angle < 0: angle += math.pi
                if angle >= math.pi - 1e-4: angle = 0
                nx, ny = -math.sin(angle), math.cos(angle)
                dist = nx * p1.X + ny * p1.Y
                return (round(angle, 2), round(dist, 1))

            plane_z = view.GenLevel.Elevation if hasattr(view, 'GenLevel') and view.GenLevel else 0.0
            created, skipped = [], []

            opts = Options()
            opts.ComputeReferences = True
            opts.View = view

            TransactionManager.Instance.EnsureInTransaction(doc)
            try:
                collinear_groups = {}
                for w in walls:
                    collinear_groups.setdefault(get_collinear_key(w), []).append(w)

                for key, group_walls in collinear_groups.items():
                    base_curve = group_walls[0].Location.Curve
                    p1, p2 = base_curve.GetEndPoint(0), base_curve.GetEndPoint(1)
                    axis_vec = XYZ(p2.X - p1.X, p2.Y - p1.Y, 0).Normalize()
                    
                    nx, ny = 0, 0
                    for w in group_walls:
                        if hasattr(w, "Orientation"):
                            nx += w.Orientation.X
                            ny += w.Orientation.Y
                            
                    norm_len = math.sqrt(nx**2 + ny**2)
                    if norm_len > 1e-5:
                        out_normal = XYZ(nx/norm_len, ny/norm_len, 0)
                    else:
                        out_normal = XYZ(-axis_vec.Y, axis_vec.X, 0)
                        
                    if mode == "interior":
                        out_normal = XYZ(-out_normal.X, -out_normal.Y, 0)

                    candidates = []
                    for w in group_walls:
                        # 1. Basic Wall & Opening geometry extraction
                        try:
                            geom = w.get_Geometry(opts)
                            if geom:
                                for obj in geom:
                                    if isinstance(obj, Solid):
                                        for face in obj.Faces:
                                            if isinstance(face, PlanarFace) and abs(abs(face.FaceNormal.DotProduct(axis_vec)) - 1.0) < 1e-2:
                                                if face.Reference:
                                                    candidates.append((face.Origin.DotProduct(axis_vec), face.Reference))
                        except: pass

                        # 2. Inserts (Doors & Windows)
                        try:
                            for i_id in w.FindInserts(True, True, True, True):
                                inst = doc.GetElement(i_id)
                                if isinstance(inst, FamilyInstance):
                                    for r_type in [FamilyInstanceReferenceType.Left, FamilyInstanceReferenceType.Right]:
                                        try:
                                            for ref in inst.GetReferences(r_type):
                                                pt = inst.GetGeometryObjectFromReference(ref).Origin
                                                candidates.append((pt.DotProduct(axis_vec), ref))
                                        except: pass
                        except: pass

                        # 3. Curtain Panels
                        try:
                            if w.WallType.Kind == WallKind.Curtain and hasattr(w, "CurtainGrid") and w.CurtainGrid:
                                for panel_id in w.CurtainGrid.GetPanelIds():
                                    panel = doc.GetElement(panel_id)
                                    p_geom = panel.get_Geometry(opts)
                                    if p_geom:
                                        for obj in p_geom:
                                            if isinstance(obj, Solid):
                                                for face in obj.Faces:
                                                    if isinstance(face, PlanarFace) and abs(abs(face.FaceNormal.DotProduct(axis_vec)) - 1.0) < 1e-2:
                                                        if face.Reference:
                                                            candidates.append((face.Origin.DotProduct(axis_vec), face.Reference))
                        except: pass

                    if not candidates: continue
                    candidates.sort(key=lambda x: x[0])

                    ref_arr = ReferenceArray()
                    ordered_refs, seen = [], []
                    for proj, ref in candidates:
                        if not any(abs(s - proj) < 1e-2 for s in seen):
                            seen.append(proj)
                            ref_arr.Append(ref)
                            ordered_refs.append(ref)

                    if ref_arr.Size < 2 or abs(seen[-1] - seen[0]) < 2.0: 
                        continue

                    mid_pt = group_walls[0].Location.Curve.Evaluate(0.5, True)
                    safe_pt = XYZ(mid_pt.X + out_normal.X * offset_ft, mid_pt.Y + out_normal.Y * offset_ft, plane_z)
                    dim_line = Line.CreateBound(safe_pt, safe_pt.Add(axis_vec.Multiply(10.0)))
                    
                    try:
                        dim = doc.Create.NewDimension(view, dim_line, ref_arr, dim_type) if dim_type else doc.Create.NewDimension(view, dim_line, ref_arr)
                        if dim: created.append(dim)
                    except Exception as e:
                        skipped.append("Chain: " + str(e))

                    if ref_arr.Size > 2:
                        overall_arr = ReferenceArray()
                        overall_arr.Append(ordered_refs[0])
                        overall_arr.Append(ordered_refs[-1])
                        
                        overall_safe_pt = safe_pt.Add(out_normal.Multiply(offset_ft))
                        o_line = Line.CreateBound(overall_safe_pt, overall_safe_pt.Add(axis_vec.Multiply(10.0)))
                        
                        try:
                            o_dim = doc.Create.NewDimension(view, o_line, overall_arr, dim_type) if dim_type else doc.Create.NewDimension(view, o_line, overall_arr)
                            if o_dim: created.append(o_dim)
                        except Exception as e:
                            skipped.append("Overall: " + str(e))

            except Exception as ex:
                skipped.append(str(ex))
            finally:
                TransactionManager.Instance.TransactionTaskDone()

            OUT = {
                "status": "Success",
                "created": len(created),
                "walls_processed": len(walls),
                "skipped": skipped
            }