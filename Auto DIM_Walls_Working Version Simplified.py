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
# Inputs
# ------------------------------------------------------------
walls_input = IN[0] if len(IN) > 0 else []
offset_mm = IN[1] if len(IN) > 1 and IN[1] is not None else 500
mode_input = IN[3] if len(IN) > 3 and IN[3] is not None else "Exterior"

mode_text = str(mode_input).strip().lower()
if "interior" in mode_text:
    mode = "interior"
elif "exterior" in mode_text:
    mode = "exterior"
else:
    mode = "both"

if not isinstance(walls_input, list):
    walls_input = [walls_input]

walls = []
for item in walls_input:
    try:
        element = UnwrapElement(item)
        if isinstance(element, Wall):
            walls.append(element)
    except Exception:
        pass

if view is None or not walls:
    OUT = {
        "status": "Invalid selection or view",
        "created": 0,
        "skipped": ["Ensure walls are selected and view is valid."],
    }
else:
    offset_ft = offset_mm / 304.8

    def vector_2d(a, b):
        return XYZ(b.X - a.X, b.Y - a.Y, 0)

    def length_2d(v):
        return math.sqrt(v.X * v.X + v.Y * v.Y)

    def normalize_2d(v):
        length = length_2d(v)
        if length < 1e-9:
            return XYZ(0, 0, 0)
        return XYZ(v.X / length, v.Y / length, 0)

    def perp_2d(v):
        return XYZ(-v.Y, v.X, 0)

    def wall_axis(wall):
        try:
            curve = wall.Location.Curve
            if isinstance(curve, Line):
                direction = curve.Direction
                return normalize_2d(XYZ(direction.X, direction.Y, 0))
            start_pt = curve.Evaluate(0.0, True)
            end_pt = curve.Evaluate(1.0, True)
            return normalize_2d(vector_2d(start_pt, end_pt))
        except Exception:
            return XYZ(0, 0, 0)

    midpoints = []
    for w in walls:
        try:
            midpoints.append(w.Location.Curve.Evaluate(0.5, True))
        except Exception:
            pass

    box_centroid = XYZ(0, 0, 0)
    if midpoints:
        box_centroid = XYZ(sum(p.X for p in midpoints) / len(midpoints), sum(p.Y for p in midpoints) / len(midpoints), 0)

    plane_z = 0.0
    try:
        plane_z = view.GenLevel.Elevation
    except Exception:
        pass

    created = []
    skipped = []

    TransactionManager.Instance.EnsureInTransaction(doc)
    try:
        # Group walls by alignment angle
        axis_groups = {}
        for w in walls:
            ax = wall_axis(w)
            if length_2d(ax) < 1e-6:
                continue
            key = round(math.atan2(ax.Y, ax.X) % math.pi, 3)
            if key not in axis_groups:
                axis_groups[key] = {'axis': ax, 'walls': []}
            axis_groups[key]['walls'].append(w)

        for key, group in axis_groups.items():
            axis = group['axis']
            group_walls = group['walls']
            axis_3d = XYZ(axis.X, axis.Y, 0)
            
            normal = perp_2d(axis)
            normal_3d = XYZ(normal.X, normal.Y, 0)
            
            # Process each wall independently or in its alignment group to ensure partitions aren't skipped
            for w in group_walls:
                try:
                    w_curve = w.Location.Curve
                    wall_mid = w_curve.Evaluate(0.5, True)
                except Exception:
                    continue

                vec_from_center = vector_2d(box_centroid, wall_mid)
                curr_normal = XYZ(normal.X, normal.Y, 0)
                if curr_normal.DotProduct(vec_from_center) < 0:
                    curr_normal = XYZ(-curr_normal.X, -curr_normal.Y, 0)

                if mode == "interior":
                    curr_normal = XYZ(-curr_normal.X, -curr_normal.Y, 0)

                # Extract candidates for this specific wall / alignment string
                candidates = []
                for target_w in group_walls:
                    try:
                        opts = Options()
                        opts.ComputeReferences = True
                        geom = target_w.get_Geometry(opts)
                        if geom:
                            for obj in geom:
                                if isinstance(obj, Solid):
                                    for face in obj.Faces:
                                        if isinstance(face, PlanarFace):
                                            if abs(abs(face.FaceNormal.DotProduct(axis_3d)) - 1.0) < 1e-2:
                                                proj = face.Origin.DotProduct(axis_3d)
                                                if face.Reference is not None:
                                                    candidates.append((proj, face.Reference))
                    except Exception:
                        pass

                if not candidates:
                    continue

                # Sort and deduplicate
                candidates.sort(key=lambda x: x[0])

                ref_arr = ReferenceArray()
                ordered_refs = []
                seen_projs = []
                tol = 1e-2 
                
                for proj, r in candidates:
                    if not any(abs(sp - proj) < tol for sp in seen_projs):
                        seen_projs.append(proj)
                        ref_arr.Append(r)
                        ordered_refs.append(r)

                if ref_arr.Size < 2:
                    continue

                # 1. Detailed Dimension Chain
                dim_pt = XYZ(wall_mid.X + curr_normal.X * offset_ft, wall_mid.Y + curr_normal.Y * offset_ft, plane_z)
                dim_line = Line.CreateBound(dim_pt, dim_pt.Add(axis.Multiply(10.0)))
                try:
                    dim = doc.Create.NewDimension(view, dim_line, ref_arr)
                    if dim is not None:
                        created.append(dim)
                except Exception as e:
                    skipped.append("Chain: {}".format(str(e)))

                # 2. Overall Dimension Tier
                if ref_arr.Size > 2:
                    if abs(seen_projs[-1] - seen_projs[0]) > 0.1:
                        overall_arr = ReferenceArray()
                        overall_arr.Append(ordered_refs[0])
                        overall_arr.Append(ordered_refs[-1])
                        
                        overall_pt = XYZ(wall_mid.X + curr_normal.X * (offset_ft * 2.2), wall_mid.Y + curr_normal.Y * (offset_ft * 2.2), plane_z)
                        overall_line = Line.CreateBound(overall_pt, overall_pt.Add(axis.Multiply(10.0)))
                        try:
                            o_dim = doc.Create.NewDimension(view, overall_line, overall_arr)
                            if o_dim is not None:
                                created.append(o_dim)
                        except Exception as e:
                            skipped.append("Overall: {}".format(str(e)))

    except Exception as ex:
        skipped.append(str(ex))
    finally:
        TransactionManager.Instance.TransactionTaskDone()

    OUT = {
        "status": "Success" if created else "No dimensions created",
        "created": len(created),
        "walls_received": len(walls),
        "mode": mode,
        "skipped": skipped,
    }