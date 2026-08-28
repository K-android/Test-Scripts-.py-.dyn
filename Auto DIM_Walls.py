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
openings_input = IN[2] if len(IN) > 2 and IN[2] is not None else []
mode_input = IN[3] if len(IN) > 3 and IN[3] is not None else "Both"
# New boolean input: include_thickness dims (False by default)
include_thickness = IN[4] if len(IN) > 4 and IN[4] is not None else False
try:
    include_thickness = bool(include_thickness)
except Exception:
    include_thickness = False

mode_text = str(mode_input).strip().lower()
if "interior" in mode_text:
    mode = "interior"
elif "exterior" in mode_text:
    mode = "exterior"
else:
    mode = "both"

if not isinstance(walls_input, list):
    walls_input = [walls_input]
if not isinstance(openings_input, list):
    openings_input = [openings_input]

walls = []
for item in walls_input:
    try:
        element = UnwrapElement(item)
        if isinstance(element, Wall):
            walls.append(element)
    except Exception:
        pass

openings = []
for item in openings_input:
    try:
        element = UnwrapElement(item)
        if element is not None:
            openings.append(element)
    except Exception:
        pass

if view is None or view.ViewType not in [
    ViewType.FloorPlan,
    ViewType.CeilingPlan,
    ViewType.Section,
    ViewType.Elevation,
    ViewType.Detail,
    ViewType.DraftingView,
]:
    OUT = {
        "status": "Invalid view",
        "created": 0,
        "skipped": ["Dimension creation requires a plan, section, elevation, or drafting view."],
        "mode": mode,
        "view_type": str(view.ViewType) if view is not None else "None",
    }
elif not walls:
    OUT = {
        "status": "No walls selected",
        "created": 0,
        "skipped": ["No valid wall elements were supplied."],
        "mode": mode,
        "view_type": str(view.ViewType),
    }
else:
    offset_ft = offset_mm / 304.8

    # -----------------------------
    # Stable baseline helpers
    # -----------------------------
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

    def direction_key(v):
        return round(math.atan2(v.Y, v.X) % math.pi, 6)

    def make_ref_array(refs):
        ref_array = ReferenceArray()
        for ref in refs:
            if ref is not None:
                ref_array.Append(ref)
        return ref_array

    def ref_key(ref):
        # Prefer the stable representation, but fall back to a string form so
        # references that don't support ConvertToStableRepresentation can still
        # be used for uniqueness checks.
        try:
            key = ref.ConvertToStableRepresentation(doc)
            if key:
                return key
        except Exception:
            pass
        try:
            return str(ref)
        except Exception:
            return None

    def wall_axis(wall):
        try:
            curve = wall.Location.Curve
            # Prefer direct direction for Line; otherwise approximate using endpoints
            if isinstance(curve, Line):
                direction = curve.Direction
                return normalize_2d(XYZ(direction.X, direction.Y, 0))
            # For arcs or complex curves, approximate axis by start->end vector
            try:
                start_pt = curve.Evaluate(0.0, True)
                end_pt = curve.Evaluate(1.0, True)
                return normalize_2d(vector_2d(start_pt, end_pt))
            except Exception:
                return XYZ(0, 0, 0)
        except Exception:
            return XYZ(0, 0, 0)

    def classify_wall_side(wall, axis):
        try:
            midpoint = wall.Location.Curve.Evaluate(0.5, True)
            normal = perp_2d(axis)
            for shell_type in [ShellLayerType.Exterior, ShellLayerType.Interior]:
                refs = HostObjectUtils.GetSideFaces(wall, shell_type)
                points = []
                for ref in refs:
                    try:
                        obj = wall.GetGeometryObjectFromReference(ref)
                        if isinstance(obj, PlanarFace):
                            points.append(obj.Origin)
                    except Exception:
                        pass
                if points:
                    avg = XYZ(
                        sum(p.X for p in points) / len(points),
                        sum(p.Y for p in points) / len(points),
                        midpoint.Z,
                    )
                    delta = vector_2d(midpoint, avg)
                    if length_2d(delta) > 1e-6:
                        if delta.X * normal.X + delta.Y * normal.Y >= 0:
                            return "exterior", normal
                        return "interior", normal
            return None, normal
        except Exception:
            return None, perp_2d(axis)

    def wall_candidate_refs(wall, axis):
        curve = wall.Location.Curve
        start_pt = curve.Evaluate(0.0, True)
        end_pt = curve.Evaluate(1.0, True)
        axis_3d = XYZ(axis.X, axis.Y, 0)
        start_proj = start_pt.DotProduct(axis_3d)
        end_proj = end_pt.DotProduct(axis_3d)

        refs = []
        for ref_type in [
            FamilyInstanceReferenceType.StrongReference,
            FamilyInstanceReferenceType.WeakReference,
            FamilyInstanceReferenceType.Left,
            FamilyInstanceReferenceType.Right,
        ]:
            try:
                for ref in wall.GetReferences(ref_type):
                    refs.append(ref)
            except Exception:
                pass

        # ShellLayerType may vary across Revit versions; build a safe list
        shell_types = [ShellLayerType.Exterior, ShellLayerType.Interior]
        try:
            struct = getattr(ShellLayerType, 'Structure', None)
            if struct is not None:
                shell_types.append(struct)
        except Exception:
            pass
        for shell_type in shell_types:
            try:
                for ref in HostObjectUtils.GetSideFaces(wall, shell_type):
                    refs.append(ref)
            except Exception:
                pass

        candidates = []
        for ref in refs:
            if ref is None:
                continue
            try:
                obj = wall.GetGeometryObjectFromReference(ref)
            except Exception:
                obj = None

            point = None
            if isinstance(obj, PlanarFace):
                point = obj.Origin
            elif isinstance(obj, Edge):
                try:
                    point = obj.AsCurve().Evaluate(0.5, True)
                except Exception:
                    point = None
            elif hasattr(obj, "Origin"):
                point = obj.Origin

            if point is None:
                continue

            proj = point.DotProduct(axis_3d)
            if abs(proj - start_proj) < 1e-4 or abs(proj - end_proj) < 1e-4:
                candidates.append((proj, ref))

        if len(candidates) < 2:
            fallback = []
            for ref in refs:
                if ref is None:
                    continue
                try:
                    obj = wall.GetGeometryObjectFromReference(ref)
                except Exception:
                    obj = None
                point = None
                if isinstance(obj, PlanarFace):
                    point = obj.Origin
                elif isinstance(obj, Edge):
                    try:
                        point = obj.AsCurve().Evaluate(0.5, True)
                    except Exception:
                        point = None
                elif hasattr(obj, "Origin"):
                    point = obj.Origin
                if point is not None:
                    fallback.append((point.DotProduct(axis_3d), ref))
            candidates = sorted(fallback, key=lambda item: item[0])

        # build unique refs from candidates
        unique = []
        seen = set()
        for proj, ref in sorted(candidates, key=lambda item: item[0]):
            key = ref_key(ref)
            if key is None or key in seen:
                continue
            seen.add(key)
            unique.append(ref)

        # If we still don't have two references, try a geometry-based fallback using face references
        if len(unique) < 2:
            try:
                opts = Options()
                opts.ComputeReferences = True
                geom = wall.get_Geometry(opts)
                geom_faces = []
                if geom is not None:
                    for geomObj in geom:
                        try:
                            if isinstance(geomObj, Solid):
                                for face in geomObj.Faces:
                                    try:
                                        if isinstance(face, PlanarFace):
                                            pt = face.Origin
                                            geom_faces.append((pt.DotProduct(axis_3d), face.Reference))
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                # sort and take min/max along axis
                geom_faces = sorted(geom_faces, key=lambda item: item[0])
                for proj, ref in geom_faces:
                    key = ref_key(ref)
                    if key is None or key in seen:
                        continue
                    seen.add(key)
                    unique.append(ref)
                    if len(unique) >= 2:
                        break
            except Exception:
                pass

        if len(unique) < 2:
            return []
        return [unique[0], unique[-1]]

    def opening_jamb_refs(opening, axis):
        refs = []
        for ref_type in [
            FamilyInstanceReferenceType.Left,
            FamilyInstanceReferenceType.Right,
            FamilyInstanceReferenceType.StrongReference,
            FamilyInstanceReferenceType.WeakReference,
        ]:
            try:
                for ref in opening.GetReferences(ref_type):
                    refs.append(ref)
            except Exception:
                pass

        valid = []
        seen = set()
        for ref in refs:
            if ref is None:
                continue
            key = ref_key(ref)
            if key is None or key in seen:
                continue
            seen.add(key)
            valid.append(ref)

        if len(valid) < 2:
            return []

        def proj(ref):
            try:
                obj = opening.GetGeometryObjectFromReference(ref)
                if isinstance(obj, PlanarFace):
                    return obj.Origin.DotProduct(XYZ(axis.X, axis.Y, 0))
                if hasattr(obj, "Origin"):
                    return obj.Origin.DotProduct(XYZ(axis.X, axis.Y, 0))
            except Exception:
                pass
            return 0.0

        valid.sort(key=proj)
        return [valid[0], valid[-1]]

    def hosted_openings(wall):
        result = []
        for opening in openings:
            try:
                if opening.Host is not None and opening.Host.Id == wall.Id:
                    result.append(opening)
            except Exception:
                pass
        return result

    def safe_dimension(line, ref_array):
        # Ensure there are at least two references
        if ref_array is None or ref_array.Size < 2:
            return None
        try:
            dim = doc.Create.NewDimension(view, line, ref_array)
            return dim
        except Exception as e:
            # Record the error so the caller can report why dimension creation failed
            try:
                skipped.append("Dimension creation failed: {0}".format(str(e)))
            except Exception:
                pass
            return None

    # Prepare containers
    created = []
    skipped = []
    diagnostics = []  # collect per-wall diagnostic info for debugging
    detailed_report = []  # full per-wall verbose data for logging

    # Split walls into linear (simple) and advanced sets
    linear_walls = []
    advanced_walls = []
    for w in walls:
        try:
            c = w.Location.Curve
            if isinstance(c, Line):
                linear_walls.append(w)
            else:
                advanced_walls.append(w)
        except Exception:
            # if we can't read the curve, treat as advanced
            advanced_walls.append(w)

    try:
        diagnostics.append({"stage": "split", "linear_count": len(linear_walls), "advanced_count": len(advanced_walls)})
    except Exception:
        pass

    # Determine the sketch plane Z (prefer view's level elevation or sketch plane)
    try:
        plane_z = None
        try:
            # Plan/ceiling views usually have a GenLevel with an Elevation
            plane_z = view.GenLevel.Elevation
        except Exception:
            pass
        if plane_z is None:
            try:
                sp = view.SketchPlane
                if sp is not None:
                    plane_z = sp.GetPlane().Origin.Z
            except Exception:
                pass
    except Exception:
        plane_z = None

    TransactionManager.Instance.EnsureInTransaction(doc)
    try:
        # ------------------
        # Fast path: linear walls only (grouped, chained dims corner-to-corner)
        # ------------------
        # Build groups of collinear linear walls and create chained corner-to-corner dims
        try:
            grouped_linear = {}
            for w in linear_walls:
                try:
                    ax = wall_axis(w)
                    if length_2d(ax) < 1e-9:
                        continue
                    grouped_linear.setdefault(direction_key(ax), []).append((w, ax))
                except Exception:
                    continue

            for group_key, items in grouped_linear.items():
                # pick representative axis
                axis = items[0][1]
                axis_3d = XYZ(axis.X, axis.Y, 0)
                normal = perp_2d(axis)
                # collect refs and projected positions
                collected = []  # (proj, ref, source_wall)
                wall_map = {}
                for wall, _ax in items:
                    try:
                        refs = wall_candidate_refs(wall, axis)
                        wall_map[wall.Id.IntegerValue] = wall
                        for ref in refs:
                            try:
                                obj = wall.GetGeometryObjectFromReference(ref)
                            except Exception:
                                obj = None
                            pt = None
                            if isinstance(obj, PlanarFace):
                                pt = obj.Origin
                            elif isinstance(obj, Edge):
                                try:
                                    pt = obj.AsCurve().Evaluate(0.5, True)
                                except Exception:
                                    pt = None
                            elif hasattr(obj, 'Origin'):
                                pt = obj.Origin
                            if pt is not None:
                                proj = pt.DotProduct(axis_3d)
                                collected.append((proj, ref, wall))
                        # include opening jamb refs as breakpoints
                        for opening in hosted_openings(wall):
                            try:
                                orefs = opening_jamb_refs(opening, axis)
                                for r in orefs:
                                    try:
                                        obj = opening.GetGeometryObjectFromReference(r)
                                    except Exception:
                                        obj = None
                                    pt = None
                                    if hasattr(obj, 'Origin'):
                                        pt = obj.Origin
                                    if pt is not None:
                                        proj = pt.DotProduct(axis_3d)
                                        collected.append((proj, r, wall))
                            except Exception:
                                pass
                    except Exception:
                        pass

                if not collected:
                    continue

                # sort and unique by stable key
                collected_sorted = sorted(collected, key=lambda it: it[0])
                unique_ordered = []
                seen_keys = set()
                for proj, ref, src in collected_sorted:
                    k = ref_key(ref)
                    if k is None or k in seen_keys:
                        continue
                    seen_keys.add(k)
                    unique_ordered.append((proj, ref, src))

                if len(unique_ordered) < 2:
                    continue

                # build reference array in order
                # Prefer edge references when face references lead to interior/thickness measurements
                preferred_refs = []
                for proj, ref, src in unique_ordered:
                    pref = ref
                    try:
                        obj = src.GetGeometryObjectFromReference(ref)
                    except Exception:
                        obj = None
                    # if it's a face, try to find the closest boundary edge and use its reference
                    if isinstance(obj, PlanarFace):
                        try:
                            best_edge_ref = None
                            best_dist = None
                            for loop in obj.EdgeLoops:
                                for edge in loop:
                                    try:
                                        mid = edge.AsCurve().Evaluate(0.5, True)
                                        d = abs(mid.DotProduct(axis_3d) - proj)
                                        if best_dist is None or d < best_dist:
                                            best_dist = d
                                            best_edge_ref = edge.Reference
                                    except Exception:
                                        pass
                            if best_edge_ref is not None:
                                pref = best_edge_ref
                        except Exception:
                            pass
                    preferred_refs.append((proj, pref, src))

                # filter out refs that collapse to the same projected point (avoid zero-length segments)
                filtered = []
                seen_coords = []
                tol = 1e-4  # feet
                z_for_proj = plane_z if plane_z is not None else None
                for proj, ref, src in preferred_refs:
                    pt = None
                    try:
                        obj = src.GetGeometryObjectFromReference(ref)
                        if isinstance(obj, PlanarFace):
                            pt = obj.Origin
                        elif isinstance(obj, Edge):
                            pt = obj.AsCurve().Evaluate(0.5, True)
                        elif hasattr(obj, 'Origin'):
                            pt = obj.Origin
                    except Exception:
                        pt = None
                    if pt is None:
                        continue
                    z = z_for_proj if z_for_proj is not None else pt.Z
                    p2d = (round(pt.X, 6), round(pt.Y, 6))
                    skip = False
                    for sx, sy in seen_coords:
                        dx = sx - p2d[0]
                        dy = sy - p2d[1]
                        if (dx*dx + dy*dy) < (tol*tol):
                            skip = True
                            break
                    if skip:
                        continue
                    seen_coords.append(p2d)
                    filtered.append((proj, ref, src, pt))

                ordered_refs = [it[1] for it in filtered]
                ref_array = make_ref_array(ordered_refs)

                # optional debug: draw small crosses at selected ref points if user requests
                debug_points = IN[5] if len(IN) > 5 and IN[5] is not None else False
                try:
                    debug_points = bool(debug_points)
                except Exception:
                    debug_points = False
                if debug_points:
                    try:
                        for _, _ref, _src, _pt in filtered:
                            try:
                                z = plane_z if plane_z is not None else _pt.Z
                                p = XYZ(_pt.X, _pt.Y, z)
                                s = 0.1
                                l1 = Line.CreateBound(XYZ(p.X - s, p.Y, p.Z), XYZ(p.X + s, p.Y, p.Z))
                                l2 = Line.CreateBound(XYZ(p.X, p.Y - s, p.Z), XYZ(p.X, p.Y + s, p.Z))
                                doc.Create.NewDetailCurve(view, l1)
                                doc.Create.NewDetailCurve(view, l2)
                            except Exception:
                                pass
                    except Exception:
                        pass

                # compute dimension line position and direction
                try:
                    # compute mid point between first and last
                    # Use the filtered points (these are the actual points used for the chained refs)
                    if len(filtered) < 2:
                        continue
                    first_pt = filtered[0][3]
                    last_pt = filtered[-1][3]
                    if first_pt is None or last_pt is None:
                        continue

                    z = plane_z if plane_z is not None else (first_pt.Z + last_pt.Z) / 2.0
                    p1o = XYZ(first_pt.X, first_pt.Y, z)
                    p2o = XYZ(last_pt.X, last_pt.Y, z)
                    dim_mid = XYZ((p1o.X + p2o.X) / 2.0, (p1o.Y + p2o.Y) / 2.0, z)
                    dim_dir = normalize_2d(axis)
                    # compute group centroid (average of wall midpoints) to decide outward normal
                    try:
                        cent_x = 0.0
                        cent_y = 0.0
                        count_cent = 0
                        for w, _ax in items:
                            try:
                                wm = w.Location.Curve.Evaluate(0.5, True)
                                cent_x += wm.X
                                cent_y += wm.Y
                                count_cent += 1
                            except Exception:
                                pass
                        if count_cent > 0:
                            group_centroid = XYZ(cent_x / count_cent, cent_y / count_cent, z)
                        else:
                            group_centroid = dim_mid
                    except Exception:
                        group_centroid = dim_mid
                    # choose normal pointing away from centroid
                    normal_dir = perp_2d(dim_dir)
                    vec_out = XYZ(dim_mid.X - group_centroid.X, dim_mid.Y - group_centroid.Y, 0)
                    try:
                        if (normal_dir.X * vec_out.X + normal_dir.Y * vec_out.Y) < 0:
                            normal_dir = XYZ(-normal_dir.X, -normal_dir.Y, 0)
                    except Exception:
                        pass
                    line_start = dim_mid.Add(normal_dir.Multiply(offset_ft))
                    line_end = line_start.Add(dim_dir.Multiply(10.0))

                    # create chained dimension (corner-to-corner; Revit will list segment lengths between refs)
                    chained = safe_dimension(Line.CreateBound(line_start, line_end), ref_array)
                    if chained is not None:
                        created.append(chained)
                        # mark walls as created in the report
                        for _, _, src in unique_ordered:
                            try:
                                wall_entry = {"wall_id": getattr(src.Id, 'IntegerValue', str(src.Id)), "path": "linear_group", "created": True}
                                wall_entry['unique_refs_count'] = len(unique_ordered)
                                wall_entry['unique_ref_keys'] = [ref_key(r[1]) for r in unique_ordered]
                                detailed_report.append(wall_entry)
                            except Exception:
                                pass
                except Exception:
                    pass

            # clear linear_walls so subsequent per-wall loop does nothing (we handled groups)
            linear_walls = []
        except Exception:
            pass

        for wall in linear_walls:
            wall_entry = {"wall_id": getattr(wall.Id, 'IntegerValue', str(wall.Id)), "path": "linear", "created": False}
            try:
                axis = wall_axis(wall)
                wall_entry['axis'] = (axis.X, axis.Y, axis.Z)
                normal = perp_2d(axis)
                wall_entry['normal'] = (normal.X, normal.Y, normal.Z)
                if length_2d(axis) < 1e-9:
                    wall_entry['reason'] = 'zero_axis'
                    diagnostics.append({"wall_id": wall_entry['wall_id'], "group_skip_reason": "zero_axis"})
                    detailed_report.append(wall_entry)
                    continue

                # simple candidate refs
                wall_refs = wall_candidate_refs(wall, axis)
                wall_entry['wall_refs_len'] = len(wall_refs)

                if len(wall_refs) >= 2:
                    wall_mid = wall.Location.Curve.Evaluate(0.5, True)
                    wall_entry['mid'] = (wall_mid.X, wall_mid.Y, wall_mid.Z)
                    normal = perp_2d(axis)
                    ordered = []
                    for ref in wall_refs:
                        try:
                            obj = wall.GetGeometryObjectFromReference(ref)
                            pt = None
                            if isinstance(obj, PlanarFace):
                                pt = obj.Origin
                            elif hasattr(obj, 'Origin'):
                                pt = obj.Origin
                            if pt is not None:
                                ordered.append((pt.DotProduct(XYZ(axis.X, axis.Y, 0)), ref))
                        except Exception:
                            pass
                    ordered = sorted(ordered, key=lambda it: it[0])
                    unique = []
                    seen = set()
                    for proj, ref in ordered:
                        k = ref_key(ref)
                        if k is None or k in seen:
                            continue
                        seen.add(k)
                        unique.append(ref)

                    wall_entry['unique_refs_count'] = len(unique)
                    wall_entry['unique_ref_keys'] = [ref_key(r) for r in unique]

                    if len(unique) >= 2:
                        # Compute exact axis from the two reference points so the dimension aligns with refs
                        try:
                            p_objs = []
                            for r in [unique[0], unique[-1]]:
                                try:
                                    obj = wall.GetGeometryObjectFromReference(r)
                                except Exception:
                                    obj = None
                                pt = None
                                if isinstance(obj, PlanarFace):
                                    pt = obj.Origin
                                elif isinstance(obj, Edge):
                                    try:
                                        pt = obj.AsCurve().Evaluate(0.5, True)
                                    except Exception:
                                        pt = None
                                elif hasattr(obj, 'Origin'):
                                    pt = obj.Origin
                                p_objs.append(pt)

                            measured_axis = None
                            if p_objs[0] is not None and p_objs[1] is not None:
                                ref_vec = vector_2d(p_objs[0], p_objs[1])
                                if length_2d(ref_vec) > 1e-6:
                                    measured_axis = normalize_2d(ref_vec)
                        except Exception:
                            measured_axis = None

                        if measured_axis is not None:
                            use_axis = measured_axis
                            use_normal = perp_2d(use_axis)
                        else:
                            # fallback to wall axis/normal
                            use_axis = axis
                            use_normal = normal

                        # update diagnostics with chosen direction
                        wall_entry['used_axis'] = (use_axis.X, use_axis.Y, use_axis.Z)
                        wall_entry['used_normal'] = (use_normal.X, use_normal.Y, use_normal.Z)

                        # Compute exact reference points and build a dimension line perpendicular to the measured vector
                        try:
                            p1, p2 = p_objs[0], p_objs[1]
                            if p1 is not None and p2 is not None:
                                # project ref points to the view sketch/level Z (prefer view GenLevel or SketchPlane)
                                try:
                                    z = plane_z if plane_z is not None else wall_mid.Z
                                    p1p = XYZ(p1.X, p1.Y, z)
                                    p2p = XYZ(p2.X, p2.Y, z)
                                except Exception:
                                    p1p = p1
                                    p2p = p2

                                meas_vec = vector_2d(p1p, p2p)
                                meas_len = length_2d(meas_vec)
                                if meas_len > 1e-6:
                                    # compute projection of measured vector onto wall axis
                                    axis_3d = XYZ(axis.X, axis.Y, 0)
                                    parallel_len = abs(meas_vec.DotProduct(axis_3d))
                                    if parallel_len < 1e-6:
                                        # Attempt an edge-based fallback: look for boundary edges with extreme
                                        # projections along the wall axis (these are likely end edges)
                                        try:
                                            opts2 = Options()
                                            opts2.ComputeReferences = True
                                            geom2 = wall.get_Geometry(opts2)
                                            edge_refs = []
                                            if geom2 is not None:
                                                for geomObj2 in geom2:
                                                    try:
                                                        if isinstance(geomObj2, Solid):
                                                            for face in geomObj2.Faces:
                                                                try:
                                                                    for loop in face.EdgeLoops:
                                                                        for edge in loop:
                                                                            try:
                                                                                mid = edge.AsCurve().Evaluate(0.5, True)
                                                                                proj_val = mid.DotProduct(axis_3d)
                                                                                edge_refs.append((proj_val, edge.Reference))
                                                                            except Exception:
                                                                                pass
                                                                except Exception:
                                                                    pass
                                                    except Exception:
                                                        pass
                                            edge_refs = sorted(edge_refs, key=lambda it: it[0])
                                            if edge_refs and abs(edge_refs[-1][0] - edge_refs[0][0]) > 1e-6:
                                                # pick extreme edge refs
                                                cand_refs = [edge_refs[0][1], edge_refs[-1][1]]
                                                # build unique list
                                                unique = []
                                                seen2 = set()
                                                for ref in cand_refs:
                                                    k = ref_key(ref)
                                                    if k is None or k in seen2:
                                                        continue
                                                    seen2.add(k)
                                                    unique.append(ref)
                                                if len(unique) >= 2:
                                                    # recompute p_objs based on these edge refs
                                                    p_objs = []
                                                    for r_ref in unique:
                                                        try:
                                                            obj_ref = wall.GetGeometryObjectFromReference(r_ref)
                                                        except Exception:
                                                            obj_ref = None
                                                        pt_ref = None
                                                        if isinstance(obj_ref, PlanarFace):
                                                            pt_ref = obj_ref.Origin
                                                        elif isinstance(obj_ref, Edge):
                                                            try:
                                                                pt_ref = obj_ref.AsCurve().Evaluate(0.5, True)
                                                            except Exception:
                                                                pt_ref = None
                                                        elif hasattr(obj_ref, 'Origin'):
                                                            pt_ref = obj_ref.Origin
                                                        p_objs.append(pt_ref)
                                                    # if we have usable points, update p1p/p2p and meas_vec below
                                                    if p_objs[0] is not None and p_objs[1] is not None:
                                                        try:
                                                            z2 = plane_z if plane_z is not None else wall_mid.Z
                                                            p1p = XYZ(p_objs[0].X, p_objs[0].Y, z2)
                                                            p2p = XYZ(p_objs[1].X, p_objs[1].Y, z2)
                                                            meas_vec = vector_2d(p1p, p2p)
                                                            meas_len = length_2d(meas_vec)
                                                            # recompute parallel length
                                                            parallel_len = abs(meas_vec.DotProduct(axis_3d))
                                                        except Exception:
                                                            pass
                                        except Exception:
                                            pass
                                    if parallel_len < 1e-6:
                                        wall_entry['reason'] = 'zero_projected_length'
                                        detailed_report.append(wall_entry)
                                        continue
                                    proj_dir = normalize_2d(XYZ(axis_3d.X, axis_3d.Y, 0))
                                    comp = abs((meas_vec.DotProduct(axis_3d)) / (meas_len))
                                    if (not include_thickness) and (comp < 0.7):
                                        wall_entry['reason'] = 'thickness_skipped'
                                        detailed_report.append(wall_entry)
                                        continue
                                    # Use wall axis as the dimension line direction (parallel to wall)
                                    dim_dir = proj_dir
                                    dim_mid = XYZ((p1p.X + p2p.X) / 2.0, (p1p.Y + p2p.Y) / 2.0, wall_mid.Z)
                                    line_start = dim_mid.Add(perp_2d(dim_dir).Multiply(offset_ft))
                                    line_end = line_start.Add(dim_dir.Multiply(10.0))
                                    dim = safe_dimension(Line.CreateBound(line_start, line_end), make_ref_array(unique))
                                    if dim is not None:
                                        created.append(dim)
                                        wall_entry['created'] = True
                        except Exception:
                            pass

                        # if not created above, fallback to previous approach
                        if not wall_entry.get('created'):
                            try:
                                z = plane_z if plane_z is not None else wall_mid.Z
                                line_start = XYZ(wall_mid.X + use_normal.X * offset_ft, wall_mid.Y + use_normal.Y * offset_ft, z)
                                line_end = line_start.Add(use_axis.Multiply(10.0))
                                dim = safe_dimension(Line.CreateBound(line_start, line_end), make_ref_array(unique))
                                if dim is not None:
                                    created.append(dim)
                                    wall_entry['created'] = True
                            except Exception:
                                pass

                    # overall dim (prefer end refs determined earlier - avoid duplicates and zero-length)
                    try:
                        refs_for_overall = None
                        if 'unique' in locals() and len(unique) >= 2:
                            refs_for_overall = [unique[0], unique[-1]]
                        else:
                            refs_for_overall = wall_refs
                        # compute projected length between these refs to avoid zero-length or thickness dims
                        overall_refs = []
                        for ref in refs_for_overall:
                            try:
                                obj = wall.GetGeometryObjectFromReference(ref)
                            except Exception:
                                obj = None
                            pt = None
                            if isinstance(obj, PlanarFace):
                                pt = obj.Origin
                            elif isinstance(obj, Edge):
                                try:
                                    pt = obj.AsCurve().Evaluate(0.5, True)
                                except Exception:
                                    pt = None
                            elif hasattr(obj, 'Origin'):
                                pt = obj.Origin
                            overall_refs.append(pt)

                        if len(overall_refs) >= 2 and overall_refs[0] is not None and overall_refs[1] is not None:
                            z = plane_z if plane_z is not None else wall_mid.Z
                            p1o = XYZ(overall_refs[0].X, overall_refs[0].Y, z)
                            p2o = XYZ(overall_refs[1].X, overall_refs[1].Y, z)
                            overall_meas_vec = vector_2d(p1o, p2o)
                            overall_meas_len = length_2d(overall_meas_vec)
                            # skip if zero or across thickness
                            if overall_meas_len > 1e-6:
                                axis_3d = XYZ(axis.X, axis.Y, 0)
                                comp_overall = abs((overall_meas_vec.DotProduct(axis_3d)) / (overall_meas_len))
                                # if overall essentially same as per-wall measured length, skip to avoid duplication
                                if 'meas_len' in locals() and abs(overall_meas_len - meas_len) < 1e-4:
                                    pass
                                elif (not include_thickness) and (comp_overall < 0.7):
                                    pass
                                else:
                                    overall_ref_array = make_ref_array(refs_for_overall)
                                    overall_start = XYZ((p1o.X + p2o.X) / 2.0 + normal.X * offset_ft * 2.0, (p1o.Y + p2o.Y) / 2.0 + normal.Y * offset_ft * 2.0, z)
                                    overall_end = overall_start.Add(axis.Multiply(10.0))
                                    overall_dim = safe_dimension(Line.CreateBound(overall_start, overall_end), overall_ref_array)
                                    if overall_dim is not None:
                                        created.append(overall_dim)
                                        wall_entry['created'] = True
                    except Exception:
                        pass

                else:
                    wall_entry['reason'] = 'not_enough_wall_refs'
                    skipped.append("Wall {0}: no valid end references (linear)".format(wall.Id.IntegerValue))

            except Exception as e:
                wall_entry['error'] = str(e)
            detailed_report.append(wall_entry)

        # ------------------
        # Advanced path: non-linear, curtain, families
        # ------------------
        # Group advanced walls by direction to reuse logic
        grouped = {}
        for wall in advanced_walls:
            try:
                axis = wall_axis(wall)
                if length_2d(axis) < 1e-9:
                    diagnostics.append({"wall_id": wall.Id.IntegerValue, "group_skip_reason": "zero_axis_advanced"})
                    detailed_report.append({"wall_id": wall.Id.IntegerValue, "path": "advanced", "reason": "zero_axis"})
                    continue
                grouped.setdefault(direction_key(axis), []).append((wall, axis))
            except Exception as e:
                diagnostics.append({"wall_repr": str(wall), "group_error": str(e)})
                detailed_report.append({"wall_repr": str(wall), "path": "advanced", "group_error": str(e)})

        for wall_group in grouped.values():
            for wall, axis in wall_group:
                wall_entry = {"wall_id": getattr(wall.Id, 'IntegerValue', str(wall.Id)), "path": "advanced", "created": False}
                try:
                    wall_entry['axis'] = (axis.X, axis.Y, axis.Z)
                    normal = perp_2d(axis)
                    wall_entry['normal'] = (normal.X, normal.Y, normal.Z)
                    actual_side, side_vec = classify_wall_side(wall, axis)
                    wall_entry['classified_side'] = actual_side

                    # respect mode selection
                    if mode == 'interior' and actual_side is not None and actual_side != 'interior':
                        wall_entry['reason'] = 'mode_mismatch'
                        detailed_report.append(wall_entry)
                        continue
                    if mode == 'exterior' and actual_side is not None and actual_side != 'exterior':
                        wall_entry['reason'] = 'mode_mismatch'
                        detailed_report.append(wall_entry)
                        continue

                    wall_refs = wall_candidate_refs(wall, axis)
                    wall_entry['wall_refs_len'] = len(wall_refs)

                    ordered_refs = []
                    for ref in wall_refs:
                        try:
                            obj = wall.GetGeometryObjectFromReference(ref)
                            pt = None
                            if isinstance(obj, PlanarFace):
                                pt = obj.Origin
                            elif hasattr(obj, 'Origin'):
                                pt = obj.Origin
                            if pt is not None:
                                ordered_refs.append((pt.DotProduct(XYZ(axis.X, axis.Y, 0)), ref))
                        except Exception:
                            pass

                    # also include openings
                    for opening in hosted_openings(wall):
                        try:
                            opening_refs = opening_jamb_refs(opening, axis)
                            for ref in opening_refs:
                                try:
                                    obj = opening.GetGeometryObjectFromReference(ref)
                                    if obj is not None and hasattr(obj, 'Origin'):
                                        ordered_refs.append((obj.Origin.DotProduct(XYZ(axis.X, axis.Y, 0)), ref))
                                except Exception:
                                    pass
                        except Exception:
                            pass

                    unique = []
                    seen = set()
                    for proj, ref in sorted(ordered_refs, key=lambda item: item[0]):
                        k = ref_key(ref)
                        if k is None or k in seen:
                            continue
                        seen.add(k)
                        unique.append(ref)

                    wall_entry['unique_refs_count'] = len(unique)
                    wall_entry['unique_ref_keys'] = [ref_key(r) for r in unique]

                    # geometry fallback if not enough unique refs
                    if len(unique) < 2:
                        # try geometry faces
                        try:
                            opts = Options()
                            opts.ComputeReferences = True
                            geom = wall.get_Geometry(opts)
                            geom_faces = []
                            if geom is not None:
                                for geomObj in geom:
                                    try:
                                        if isinstance(geomObj, Solid):
                                            for face in geomObj.Faces:
                                                try:
                                                    if isinstance(face, PlanarFace):
                                                        pt = face.Origin
                                                        geom_faces.append((pt.DotProduct(XYZ(axis.X, axis.Y, 0)), face.Reference))
                                                except Exception:
                                                    pass
                                    except Exception:
                                        pass
                            geom_faces = sorted(geom_faces, key=lambda item: item[0])
                            for proj, ref in geom_faces:
                                k = ref_key(ref)
                                if k is None or k in seen:
                                    continue
                                seen.add(k)
                                unique.append(ref)
                                if len(unique) >= 2:
                                    break
                        except Exception:
                            pass

                    wall_entry['post_fallback_unique_count'] = len(unique)
                    wall_entry['post_fallback_keys'] = [ref_key(r) for r in unique]

                    if len(unique) >= 2:
                        wall_mid = wall.Location.Curve.Evaluate(0.5, True)
                        wall_entry['mid'] = (wall_mid.X, wall_mid.Y, wall_mid.Z)
                        normal = perp_2d(axis)

                        # Compute axis from refs if possible to align the dimension
                        try:
                            p_objs = []
                            for r in [unique[0], unique[-1]]:
                                try:
                                    obj = wall.GetGeometryObjectFromReference(r)
                                except Exception:
                                    obj = None
                                pt = None
                                if isinstance(obj, PlanarFace):
                                    pt = obj.Origin
                                elif isinstance(obj, Edge):
                                    try:
                                        pt = obj.AsCurve().Evaluate(0.5, True)
                                    except Exception:
                                        pt = None
                                elif hasattr(obj, 'Origin'):
                                    pt = obj.Origin
                                p_objs.append(pt)

                            measured_axis = None
                            if p_objs[0] is not None and p_objs[1] is not None:
                                ref_vec = vector_2d(p_objs[0], p_objs[1])
                                if length_2d(ref_vec) > 1e-6:
                                    measured_axis = normalize_2d(ref_vec)
                        except Exception:
                            measured_axis = None

                        if measured_axis is not None:
                            use_axis = measured_axis
                            use_normal = perp_2d(use_axis)
                        else:
                            use_axis = axis
                            use_normal = normal

                        wall_entry['used_axis'] = (use_axis.X, use_axis.Y, use_axis.Z)
                        wall_entry['used_normal'] = (use_normal.X, use_normal.Y, use_normal.Z)

                        # Build dimension line perpendicular to measured vector between refs
                        try:
                            p1, p2 = p_objs[0], p_objs[1]
                            if p1 is not None and p2 is not None:
                                # project ref points to the view sketch/level Z (prefer view GenLevel or SketchPlane)
                                try:
                                    z = plane_z if plane_z is not None else wall_mid.Z
                                    p1p = XYZ(p1.X, p1.Y, z)
                                    p2p = XYZ(p2.X, p2.Y, z)
                                except Exception:
                                    p1p = p1
                                    p2p = p2

                                meas_vec = vector_2d(p1p, p2p)
                                meas_len = length_2d(meas_vec)
                                if meas_len > 1e-6:
                                    # compute projection of measured vector onto wall axis
                                    axis_3d = XYZ(axis.X, axis.Y, 0)
                                    parallel_len = abs(meas_vec.DotProduct(axis_3d))
                                    if parallel_len < 1e-6:
                                        # Try edge-based fallback for end-edge references
                                        try:
                                            opts2 = Options()
                                            opts2.ComputeReferences = True
                                            geom2 = wall.get_Geometry(opts2)
                                            edge_refs = []
                                            if geom2 is not None:
                                                for geomObj2 in geom2:
                                                    try:
                                                        if isinstance(geomObj2, Solid):
                                                            for face in geomObj2.Faces:
                                                                try:
                                                                    for loop in face.EdgeLoops:
                                                                        for edge in loop:
                                                                            try:
                                                                                mid = edge.AsCurve().Evaluate(0.5, True)
                                                                                proj_val = mid.DotProduct(axis_3d)
                                                                                edge_refs.append((proj_val, edge.Reference))
                                                                            except Exception:
                                                                                pass
                                                                except Exception:
                                                                    pass
                                                    except Exception:
                                                        pass
                                            edge_refs = sorted(edge_refs, key=lambda it: it[0])
                                            if edge_refs and abs(edge_refs[-1][0] - edge_refs[0][0]) > 1e-6:
                                                cand_refs = [edge_refs[0][1], edge_refs[-1][1]]
                                                unique = []
                                                seen2 = set()
                                                for ref in cand_refs:
                                                    k = ref_key(ref)
                                                    if k is None or k in seen2:
                                                        continue
                                                    seen2.add(k)
                                                    unique.append(ref)
                                                if len(unique) >= 2:
                                                    p_objs = []
                                                    for r_ref in unique:
                                                        try:
                                                            obj_ref = wall.GetGeometryObjectFromReference(r_ref)
                                                        except Exception:
                                                            obj_ref = None
                                                        pt_ref = None
                                                        if isinstance(obj_ref, PlanarFace):
                                                            pt_ref = obj_ref.Origin
                                                        elif isinstance(obj_ref, Edge):
                                                            try:
                                                                pt_ref = obj_ref.AsCurve().Evaluate(0.5, True)
                                                            except Exception:
                                                                pt_ref = None
                                                        elif hasattr(obj_ref, 'Origin'):
                                                            pt_ref = obj_ref.Origin
                                                        p_objs.append(pt_ref)
                                                    if p_objs[0] is not None and p_objs[1] is not None:
                                                        try:
                                                            z2 = plane_z if plane_z is not None else wall_mid.Z
                                                            p1p = XYZ(p_objs[0].X, p_objs[0].Y, z2)
                                                            p2p = XYZ(p_objs[1].X, p_objs[1].Y, z2)
                                                            meas_vec = vector_2d(p1p, p2p)
                                                            meas_len = length_2d(meas_vec)
                                                            parallel_len = abs(meas_vec.DotProduct(axis_3d))
                                                        except Exception:
                                                            pass
                                        except Exception:
                                            pass
                                    if parallel_len < 1e-6:
                                        wall_entry['reason'] = 'zero_projected_length'
                                        detailed_report.append(wall_entry)
                                        continue
                                    # compute parallel direction
                                    proj_dir = normalize_2d(XYZ(axis_3d.X, axis_3d.Y, 0))
                                    # component along axis (0..1)
                                    comp = abs((meas_vec.DotProduct(axis_3d)) / (meas_len))
                                    # if this component is small it means the measured vector is across thickness
                                    if (not include_thickness) and (comp < 0.7):
                                        wall_entry['reason'] = 'thickness_skipped'
                                        detailed_report.append(wall_entry)
                                        continue
                                    # Use wall axis as the dimension line direction (parallel to wall)
                                    dim_dir = proj_dir
                                    z = plane_z if plane_z is not None else wall_mid.Z
                                    dim_mid = XYZ((p1p.X + p2p.X) / 2.0, (p1p.Y + p2p.Y) / 2.0, z)
                                    line_start = dim_mid.Add(perp_2d(dim_dir).Multiply(offset_ft))
                                    line_end = line_start.Add(dim_dir.Multiply(10.0))
                                    dim = safe_dimension(Line.CreateBound(line_start, line_end), make_ref_array(unique))
                                    if dim is not None:
                                        created.append(dim)
                                        wall_entry['created'] = True
                                    # overall (avoid duplicate or zero-length overall dims)
                                    try:
                                        refs_for_overall = [unique[0], unique[-1]]
                                        overall_pts = []
                                        for ref in refs_for_overall:
                                            try:
                                                obj = wall.GetGeometryObjectFromReference(ref)
                                            except Exception:
                                                obj = None
                                            pt = None
                                            if isinstance(obj, PlanarFace):
                                                pt = obj.Origin
                                            elif isinstance(obj, Edge):
                                                try:
                                                    pt = obj.AsCurve().Evaluate(0.5, True)
                                                except Exception:
                                                    pt = None
                                            elif hasattr(obj, 'Origin'):
                                                pt = obj.Origin
                                            overall_pts.append(pt)
                                        if len(overall_pts) >= 2 and overall_pts[0] is not None and overall_pts[1] is not None:
                                            z2 = plane_z if plane_z is not None else dim_mid.Z
                                            p1o = XYZ(overall_pts[0].X, overall_pts[0].Y, z2)
                                            p2o = XYZ(overall_pts[1].X, overall_pts[1].Y, z2)
                                            overall_meas_vec = vector_2d(p1o, p2o)
                                            overall_meas_len = length_2d(overall_meas_vec)
                                            if overall_meas_len > 1e-6:
                                                axis_3d = XYZ(axis.X, axis.Y, 0)
                                                comp_overall = abs((overall_meas_vec.DotProduct(axis_3d)) / (overall_meas_len))
                                                if 'meas_len' in locals() and abs(overall_meas_len - meas_len) < 1e-4:
                                                    pass
                                                elif (not include_thickness) and (comp_overall < 0.7):
                                                    pass
                                                else:
                                                    overall_ref_array = make_ref_array(refs_for_overall)
                                                    overall_start = XYZ(dim_mid.X + perp_2d(dim_dir).X * offset_ft * 2.0, dim_mid.Y + perp_2d(dim_dir).Y * offset_ft * 2.0, z2)
                                                    overall_end = overall_start.Add(dim_dir.Multiply(10.0))
                                                    overall_dim = safe_dimension(Line.CreateBound(overall_start, overall_end), overall_ref_array)
                                                    if overall_dim is not None:
                                                        created.append(overall_dim)
                                                        wall_entry['created'] = True
                                    except Exception:
                                        pass
                        except Exception:
                            pass

                        # fallback to previous approach if not created
                        if not wall_entry.get('created'):
                            try:
                                z = plane_z if plane_z is not None else wall_mid.Z
                                line_start = XYZ(wall_mid.X + use_normal.X * offset_ft, wall_mid.Y + use_normal.Y * offset_ft, z)
                                line_end = line_start.Add(use_axis.Multiply(10.0))
                                dim = safe_dimension(Line.CreateBound(line_start, line_end), make_ref_array(unique))
                                if dim is not None:
                                    created.append(dim)
                                    wall_entry['created'] = True
                            except Exception:
                                pass
                    else:
                        wall_entry['reason'] = 'not_enough_refs_after_fallback'
                        skipped.append("Wall {0}: no valid end references (advanced)".format(wall.Id.IntegerValue))

                except Exception as e:
                    wall_entry['error'] = str(e)
                detailed_report.append(wall_entry)

    except Exception as ex:
        OUT = {"status": "Error", "created": 0, "skipped": [str(ex)], "mode": mode}
    finally:
        TransactionManager.Instance.TransactionTaskDone()

    # attempt to write detailed report to a debug file for inspection
    try:
        import os
        debug_path = r"C:\Users\INKN214189\.copilot\session-state\9820214e-e9b2-49ed-8ac4-75a838e7e92f\files\auto_dim_debug.txt"
        with open(debug_path, 'w') as f:
            import json
            f.write(json.dumps({"diagnostics": diagnostics, "detailed_report": detailed_report, "created_count": len(created), "skipped": skipped}, indent=2))
        diagnostics.append({"debug_file": debug_path})
    except Exception:
        pass

    OUT = {
        "status": "Success" if created else "No dimensions created",
        "created": len(created),
        "walls_received": len(walls),
        "openings_received": len(openings),
        "mode": mode,
        "skipped": skipped,
        "diagnostics": diagnostics,
        "detailed_report": detailed_report,
    }