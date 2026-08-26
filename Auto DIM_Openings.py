import clr
import sys

# Load Revit API
clr.AddReference('RevitAPI')
from Autodesk.Revit.DB import *

# Load Dynamo Transaction Managers
clr.AddReference('RevitServices')
from RevitServices.Persistence import DocumentManager
from RevitServices.Transactions import TransactionManager

doc = DocumentManager.Instance.CurrentDBDocument
view = doc.ActiveView

# 1. Safely handle the selected wall
raw_wall = IN[0]
if isinstance(raw_wall, list):
    raw_wall = raw_wall[0]
wall = UnwrapElement(raw_wall)

# 2. Convert offset slider from millimeters to internal Revit feet
offset_mm = IN[1]
offset_ft = offset_mm / 304.8

ref_array = ReferenceArray()

opt = Options()
opt.ComputeReferences = True
opt.IncludeNonVisibleObjects = True
opt.View = view

if isinstance(wall, Wall):
    try:
        # Get wall geometry line and direction
        wall_curve = wall.Location.Curve
        wall_dir = wall_curve.Direction
        wall_start = wall_curve.GetEndPoint(0)
        wall_end = wall_curve.GetEndPoint(1)
        
        perp_vec = XYZ(-wall_dir.Y, wall_dir.X, 0).Normalize()
        
        # 3. Add Wall Start Reference
        geom_elem = wall.get_Geometry(opt)
        for geom_obj in geom_elem:
            if isinstance(geom_obj, Curve):
                ref_array.Append(geom_obj.GetEndPointReference(0))
                break

        # 4. Find all doors/windows (inserts) hosted in this wall
        insert_ids = wall.FindInserts(True, True, True, True)
        
        # Sort inserts along the wall curve so dimensions are sequential
        insert_data = []
        for id in insert_ids:
            elem = doc.GetElement(id)
            # Get location point of the door/window family instance
            loc_point = elem.Location.Point
            # Project point onto wall vector to get its distance along the wall
            vec_to_elem = loc_point - wall_start
            distance = vec_to_elem.DotProduct(wall_dir)
            insert_data.append((distance, elem))
            
        # Sort by distance from start of wall
        insert_data.sort(key=lambda x: x[0])

        # Extract geometry references from the sorted openings
        for dist, opening in insert_data:
            open_geom = opening.get_Geometry(opt)
            for og in open_geom:
                if isinstance(og, Instance):
                    inst_geom = og.GetSymbolGeometry()
                    for ig in inst_geom:
                        if isinstance(ig, Solid):
                            # Grab face references representing the opening frame/sides
                            for face in ig.Faces:
                                if isinstance(face, PlanarFace):
                                    if abs(face.FaceNormal.DotProduct(wall_dir)) > 0.99:
                                        ref_array.Append(face.Reference)
                                        break

        # 5. Add Wall End Reference
        for geom_obj in geom_elem:
            if isinstance(geom_obj, Curve):
                ref_array.Append(geom_obj.GetEndPointReference(1))
                break

        # 6. Build dimension line and commit transaction
        if ref_array.Size > 2:
            start_pt = wall_start + (wall_dir * 0) + (perp_vec * offset_ft)
            end_pt = wall_end + (perp_vec * offset_ft)
            dim_line = Line.CreateBound(start_pt, end_pt)
            
            TransactionManager.Instance.EnsureInTransaction(doc)
            new_dim = doc.Create.NewDimension(view, dim_line, ref_array)
            TransactionManager.Instance.TransactionTaskDone()
            
            OUT = "Success! Dimensioned " + str(len(insert_ids)) + " openings."
        else:
            OUT = "Warning: Wall has no hosted doors or windows to dimension."

    except Exception as error:
        TransactionManager.Instance.ForceCloseTransaction()
        OUT = "Error: " + str(error)
else:
    OUT = "Please select a valid Wall element."