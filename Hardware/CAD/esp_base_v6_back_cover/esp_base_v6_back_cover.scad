// v6 rear cover: flush outside face x=0. Units mm.
// Print with outer face on bed. Ring clearance is open at bottom.
module prism(p,z,h) {translate([0,0,z]) linear_extrude(h) polygon(p);}
module finger(){
 // 3 mm projection from the inner face x=2.8.
 translate([2.6,-42.7,15.5]) cube([2.9,1.2,2.5]);
 prism([[5.5,-42.7],[5.8,-42.1],[5.5,-41.5]],15.5,2.5);
 prism([[4.66,-42.7],[4.9,-43.25],[5.26,-43.25],[5.5,-42.7]],16,1.5);
}
module assembled(){
 union(){
  difference(){
   translate([0,-46,3.15]) cube([2.8,46,14.95]);
   // Extend through y=-46 so no narrow tab remains beside the ring notch.
   translate([-1,-47,2]) cube([5,18.65,4.35]);
  }
  finger();
  translate([0,-46,0]) mirror([0,1,0]) finger();
 }
}
multmatrix([[0,1,0,46],[0,0,1,-3.15],[1,0,0,0],[0,0,0,1]]) assembled();
