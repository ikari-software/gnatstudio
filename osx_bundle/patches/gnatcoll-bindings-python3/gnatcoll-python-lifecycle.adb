------------------------------------------------------------------------------
--                             G N A T C O L L                              --
--                                                                          --
--                       Copyright (C) 2020-2021, AdaCore                   --
--                                                                          --
-- This library is free software;  you can redistribute it and/or modify it --
-- under terms of the  GNU General Public License  as published by the Free --
-- Software  Foundation;  either version 3,  or (at your  option) any later --
-- version. This library is distributed in the hope that it will be useful, --
-- but WITHOUT ANY WARRANTY;  without even the implied warranty of MERCHAN- --
-- TABILITY or FITNESS FOR A PARTICULAR PURPOSE.                            --
--                                                                          --
-- As a special exception under Section 7 of GPL version 3, you are granted --
-- additional permissions described in the GCC Runtime Library Exception,   --
-- version 3.1, as published by the Free Software Foundation.               --
--                                                                          --
-- You should have received a copy of the GNU General Public License and    --
-- a copy of the GCC Runtime Library Exception along with this program;     --
-- see the files COPYING3 and COPYING.RUNTIME respectively.  If not, see    --
-- <http://www.gnu.org/licenses/>.                                          --
--                                                                          --
------------------------------------------------------------------------------

with GNATCOLL.Python.Fileutils;
with Ada.Command_Line;
with Interfaces.C.Strings;

package body GNATCOLL.Python.Lifecycle is

   package Fileutils renames GNATCOLL.Python.Fileutils;
   package IC renames Interfaces.C;

   Finalized : Boolean := False;

   procedure Ada_Py_Set_Python_Home (Home : IC.Strings.chars_ptr);
   pragma Import (C, Ada_Py_Set_Python_Home, "ada_py_set_python_home");
   --  Stash a UTF-8 home string inside python_support.c for use by the
   --  next ada_py_initialize_and_module call.  Replaces the deprecated
   --  Py_SetPythonHome, which is a no-op on Python 3.14 in some init paths.

   procedure Ada_Py_Set_Executable (Exe : IC.Strings.chars_ptr);
   pragma Import (C, Ada_Py_Set_Executable, "ada_py_set_executable");
   --  Stash a UTF-8 executable path for PyConfig.executable at init time.

   ------------------
   -- Is_Finalized --
   ------------------

   function Is_Finalized return Boolean is
   begin
      return Finalized;
   end Is_Finalized;

   -----------------
   -- Py_Finalize --
   -----------------

   function Py_Finalize return Boolean is
      function Internal return Integer;
      pragma Import (C, Internal, "Py_FinalizeEx");

   begin
      Finalized := True;
      return Internal = 0;
   end Py_Finalize;

   -------------------
   -- Py_Initialize --
   -------------------

   procedure Py_Initialize (Initialize_Signal_Handlers : Boolean := True) is
      procedure Internal (Init_Sigs : Integer);
      pragma Import (C, Internal, "Py_InitializeEx");
   begin
      Finalized := False;
      if Initialize_Signal_Handlers then
         Internal (Init_Sigs => 1);
      else
         Internal (Init_Sigs => 0);
      end if;
   end Py_Initialize;

   ----------------------
   -- Py_SetPythonHome --
   ----------------------

   procedure Py_SetPythonHome (Home : String) is
      C_Home : IC.Strings.chars_ptr := IC.Strings.New_String (Home);
   begin
      --  Stash the home string on the C side; it's applied via PyConfig
      --  inside ada_py_initialize_and_module (the deprecated Py_SetPythonHome
      --  is silently ignored by parts of Python 3.14's init).
      Ada_Py_Set_Python_Home (C_Home);
      IC.Strings.Free (C_Home);
   end Py_SetPythonHome;

   ----------------------
   -- Py_SetExecutable --
   ----------------------

   procedure Py_SetExecutable (Executable : String) is
      C_Exe : IC.Strings.chars_ptr := IC.Strings.New_String (Executable);
   begin
      Ada_Py_Set_Executable (C_Exe);
      IC.Strings.Free (C_Exe);
   end Py_SetExecutable;

   ------------------------
   --  Py_SetProgramName --
   ------------------------

   procedure Py_SetProgramName (Name : String) is
   begin
      Py_SetProgramName (Name => Fileutils.Py_DecodeLocale (Name));
   end Py_SetProgramName;

   procedure Py_SetProgramName is
   begin
      Py_SetProgramName (Name => Ada.Command_Line.Command_Name);
   end Py_SetProgramName;

end GNATCOLL.Python.Lifecycle;
