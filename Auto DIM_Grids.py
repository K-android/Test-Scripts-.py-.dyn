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

# 1. Safely handle list of selected grids (CPython 3 strict list safety)
raw_input = IN[0]
if not isinstance(raw_input, list):
    raw_input = [raw_input]
elements = UnwrapElement(raw_input)

# 2. Convert offset slider from millimeters to internal Revit feet
offset_mm = IN[1]
offset_ft = offset_mm / 304.8

ref_array = ReferenceArray()
valid_grids = []

# 3. Filter for valid Grid elements and extract references
for e in elements:
    if isinstance(e, Grid):
        ref_array.Append(Reference(e))
        valid_grids.append(e)

# 4. Generate geometry and commit transaction
if len(valid_grids) >= 2:
    try:
        # Use the first grid curve to calculate direction and placement
        grid_curve = valid_grids[0].Curve
        g_dir = grid_curve.Direction
        
        # Calculate a perpendicular vector in the XY plane to push the dimension line out
        perp_vec = XYZ(-g_dir.Y, g_dir.X, 0).Normalize()
        
        # Calculate start and end points for the dimension string using the user's offset
        start_pt = grid_curve.GetEndPoint(0) + (perp_vec * offset_ft)
        end_pt = grid_curve.GetEndPoint(1) + (perp_vec * offset_ft)
        
        dim_line = Line.CreateBound(start_pt, end_pt)
        
        # Execute Revit Transaction safely
        TransactionManager.Instance.EnsureInTransaction(doc)
        new_dim = doc.Create.NewDimension(view, dim_line, ref_array)
        TransactionManager.Instance.TransactionTaskDone()
        
        OUT = "Success! Dimensioned " + str(len(valid_grids)) + " grids."
        
    except Exception as error:
        TransactionManager.Instance.ForceCloseTransaction()
        OUT = "Error: " + str(error)
else:
    OUT = "Error: Please select at least 2 valid grids."