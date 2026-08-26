import clr
import sys
import math

# Load Revit API
clr.AddReference('RevitAPI')
from Autodesk.Revit.DB import *

# Load Dynamo Transaction Managers
clr.AddReference('RevitServices')
from RevitServices.Persistence import DocumentManager
from RevitServices.Transactions import TransactionManager

doc = DocumentManager.Instance.CurrentDBDocument
view = doc.ActiveView

# 1. Safely handle list of selected walls (CPython 3 strict list safety)
raw_walls = IN[0]
if not isinstance(raw_walls, list):
    raw_walls = [raw_walls]
walls = UnwrapElement(raw_walls)

# 2. Convert offset slider from millimeters to internal Revit feet
offset_mm = IN[1]
offset_ft = offset_mm / 304.8

ref_array = ReferenceArray()
valid_walls_count = 0

# Set geometry options to extract accurate face references
opt = Options()
opt.ComputeReferences = True
opt.IncludeNonVisibleObjects = True
opt.View = view

if len(walls) > 0 and isinstance(walls[0], Wall):
    # 3. Establish alignment axis using the first selected wall
    base_curve = walls[0].Location.Curve
    base_dir = base_curve.Direction
    base_pt = base_curve.GetEndPoint(0)
    
    # Calculate perpendicular vector for pushing the dimension line outward
    perp_vec = XYZ(-base_dir.Y, base_dir.X, 0).Normalize()
    
    # 4. Loop through selected walls and harvest parallel exterior/interior faces
    for wall in walls:
        if isinstance(wall, Wall):
            valid_walls_count += 1
            geom_elem = wall.get_Geometry(opt)
            for geom_obj in geom_elem:
                if isinstance(geom_obj, Solid):
                    for face in geom_obj.Faces:
                        if isinstance(face, PlanarFace):
                            # Dot product check ensures we only grab parallel faces 
                            # and ignore perpendicular cross-walls.
                            if abs(face.FaceNormal.DotProduct(perp_vec)) > 0.99:
                                ref_array.Reference = face.Reference
                                ref_array.Append(face.Reference)
                                
    # 5. Build dimension line and commit transaction to Revit
    if ref_array.Size > 1:
        # Translate start point along the wall direction by user offset
        start_pt = base_pt + (base_dir * offset_ft)
        # Project outward using the perpendicular vector
        end_pt = start_pt + (perp_vec * 15) 
        dim_line = Line.CreateBound(start_pt, end_pt)
        
        TransactionManager.Instance.EnsureInTransaction(doc)
        try:
            new_dim = doc.Create.NewDimension(view, dim_line, ref_array)
            OUT = "Success! Dimensioned " + str(valid_walls_count) + " walls."
        except Exception as error:
            TransactionManager.Instance.ForceCloseTransaction()
            OUT = "Error: " + str(error)
        TransactionManager.Instance.TransactionTaskDone()
    else:
        OUT = "Error: Could not isolate parallel wall faces for dimensioning."
else:
    OUT = "Please select valid wall elements."