from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
import json
import time
import logging
import os
import sys
from datetime import datetime
import re
import argparse
import random
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def get_coordinates(address):
    """Get latitude and longitude from address"""
    try:
        # Format address to be more geocoder-friendly
        # Remove extra commas and clean up whitespace
        clean_address = re.sub(r',+', ',', address).strip()
        logger.info(f"Attempting to geocode address: {clean_address}")
        
        geolocator = Nominatim(user_agent="centurycommunities_scraper")
        location = geolocator.geocode(clean_address, timeout=10)
        
        if location:
            logger.info(f"Successfully geocoded address. Found coordinates: {location.latitude}, {location.longitude}")
            return location.latitude, location.longitude
        else:
            logger.warning(f"Could not geocode address: {clean_address}")
            # Try with just city and state
            city_state_match = re.search(r'([^,]+),\s*([A-Z]{2})', clean_address)
            if city_state_match:
                city_state = f"{city_state_match.group(1)}, {city_state_match.group(2)}"
                logger.info(f"Trying with city and state only: {city_state}")
                location = geolocator.geocode(city_state, timeout=10)
                if location:
                    logger.info(f"Successfully geocoded city/state. Found coordinates: {location.latitude}, {location.longitude}")
                    return location.latitude, location.longitude
        
        return None, None
    except (GeocoderTimedOut, GeocoderServiceError) as e:
        logger.error(f"Error getting coordinates for {address}: {str(e)}")
        return None, None

def setup_chrome_driver():
    """Set up Chrome driver with appropriate options"""
    chrome_options = Options()
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--window-size=1920,1080')
    chrome_options.add_argument('--ignore-certificate-errors')
    chrome_options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    chrome_options.page_load_strategy = 'eager'
    return webdriver.Chrome(options=chrome_options)

def extract_price(text):
    """Extract price from text"""
    if not text:
        return None
    price_match = re.search(r'\$[\d,]+', text)
    return price_match.group(0) if price_match else None

def extract_beds_baths(text):
    """Extract number of beds and baths from text"""
    if not text:
        return None, None
    beds_match = re.search(r'(\d+)\s*(?:Bedroom|Bed|BR|br)', text, re.IGNORECASE)
    baths_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:Bathroom|Bath|BA|ba)', text, re.IGNORECASE)
    beds = beds_match.group(1) if beds_match else None
    baths = baths_match.group(1) if baths_match else None
    return beds, baths

def extract_sqft(text):
    """Extract square footage from text"""
    if not text:
        return None
    sqft_match = re.search(r'([\d,]+)\s*sq(?:\.|uare)?\s*ft', text.lower())
    return sqft_match.group(1).replace(',', '') if sqft_match else None

def get_first_valid_image(container):
    """Extract first valid image from container"""
    if not container:
        return None
    for img in container.find_all('img'):
        src = img.get('src')
        if src and not src.startswith('data:'):
            if not src.startswith('http'):
                src = 'https://www.centurycommunities.com' + src
            return src
    return None

def get_floorplan_images(driver, url):
    """Get floor plan images from the detail page"""
    try:
        logger.info(f"Getting floor plan images from: {url}")
        driver.get(url)
        # Wait longer for dynamic content to load
        time.sleep(5)
        
        # Wait for floor plan tabs to be present
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CLASS_NAME, 'floor-plan-img'))
            )
        except Exception as e:
            logger.warning(f"Timeout waiting for floor plan images: {str(e)}")
        
        # Get all floor plan tabs
        tabs = driver.find_elements(By.CSS_SELECTOR, '.list-group-item')
        logger.info(f"Found {len(tabs)} floor plan tabs")
        
        floorplan_images = []
        for tab in tabs:
            try:
                # Skip the "Options" tab
                if 'Options' in tab.text:
                    continue
                
                tab_text = tab.text.strip()
                logger.info(f"Processing tab: {tab_text}")
                
                # Click the tab
                driver.execute_script("arguments[0].click();", tab)
                time.sleep(2)  # Wait for content to load
                
                # Find the active floor plan image
                active_pane = driver.find_element(By.CSS_SELECTOR, '.tab-pane.active')
                img = active_pane.find_element(By.CSS_SELECTOR, 'img.floor-plan-img')
                
                if img:
                    img_url = img.get_attribute('src')
                    if img_url and not img_url.startswith('data:'):
                        if not img_url.startswith('http'):
                            img_url = 'https://www.centurycommunities.com' + img_url
                        
                        floorplan_images.append({
                            "name": tab_text + " Floorplan",
                            "image_url": img_url
                        })
                        logger.info(f"Found floor plan image for {tab_text}: {img_url}")
            except Exception as e:
                logger.error(f"Error processing tab {tab_text}: {str(e)}")
                continue
        
        logger.info(f"Total floor plan images found: {len(floorplan_images)}")
        return floorplan_images
    except Exception as e:
        logger.error(f"Error getting floor plan images: {str(e)}")
        return []

def extract_homeplans(soup, driver):
    homeplans = []
    # Find all floor plan cards
    cards = soup.find_all('li', class_='floor_plan_contain card quick-move-in-card')
    
    seen_models = set()  # Track unique models
    
    for card in cards:
        # Get the model name
        name = card.find('span', class_='title').text.strip()
        
        # Skip if we've already processed this model
        if name in seen_models:
            continue
        seen_models.add(name)
        
        # Get the URL from the "View Details" link and add domain prefix
        url = card.find('a', class_='btn btn-primary')['href']
        if not url.startswith('http'):
            url = 'https://www.centurycommunities.com' + url
        
        # Extract details
        details = {
            "price": card.find('span', class_='price').text.strip(),
            "beds": card.find('img', alt='Bedrooms').find_next('span').text.strip(),
            "baths": card.find('img', alt='Bathrooms').find_next('span').text.strip(),
            "sqft": card.find('img', alt='Square Footage').find_next('span').text.strip(),
            "status": "Actively selling",
            "image_url": card.find('img', class_='js-img').get('src', '')
        }
        
        # Add domain prefix to image URL if needed
        if details["image_url"] and not details["image_url"].startswith('http'):
            details["image_url"] = 'https://www.centurycommunities.com' + details["image_url"]
        
        # Get floor plan images from detail page
        floorplan_images = get_floorplan_images(driver, url)
        
        # Create the homeplan object
        homeplan = {
            "name": name,
            "url": url,
            "details": details,
            "includedFeatures": [],  # Empty list as this data is not in the current HTML
            "floorplan_images": floorplan_images
        }
        
        homeplans.append(homeplan)
    
    return homeplans

def get_homesite_images(driver, url):
    """Get homesite images from the detail page"""
    try:
        logger.info(f"Getting homesite images from: {url}")
        driver.get(url)
        # Wait longer for dynamic content to load
        time.sleep(5)
        
        # Wait for photo gallery to be present
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CLASS_NAME, 'photo-gallery'))
            )
        except Exception as e:
            logger.warning(f"Timeout waiting for photo gallery: {str(e)}")
            return []
        
        # Get all carousel items
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        carousel_items = soup.find_all('div', class_='carousel-item')
        
        images = []
        count = 0
        
        for item in carousel_items:
            if count >= 12:  # Limit to 12 images
                break
                
            img = item.find('img')
            if img and img.get('src'):
                img_url = img['src']
                if not img_url.startswith('http'):
                    img_url = 'https://www.centurycommunities.com' + img_url
                images.append(img_url)
                count += 1
                logger.info(f"Found homesite image {count}: {img_url}")
        
        logger.info(f"Total homesite images found: {len(images)}")
        return images
    except Exception as e:
        logger.error(f"Error getting homesite images: {str(e)}")
        return []

def fetch_page(url, output_dir='data/centurycommunities'):
    """Fetch and parse page data"""
    driver = None
    try:
        # Generate output filename
        community_name = url.split('/')[-2]
        json_file = f"{output_dir}/json/century_{community_name}.json"
        
        logger.info(f"Processing URL: {url}")
        driver = setup_chrome_driver()
        driver.get(url)
        time.sleep(5)  # Wait for page load
        
        # Save HTML
        os.makedirs(f"{output_dir}/html", exist_ok=True)
        os.makedirs(f"{output_dir}/json", exist_ok=True)
        html_file = f"{output_dir}/html/century_{community_name}.html"
        with open(html_file, 'w', encoding='utf-8') as f:
            f.write(driver.page_source)
        logger.info(f"HTML saved to: {html_file}")

        # Parse data
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        data = {
            "timestamp": datetime.now().isoformat(),
            "builder": "Century Communities",
            "name": None,
            "status": "Active",
            "url": url,
            "price_from": None,
            "address": None,
            "phone": None,
            "description": None,
            "images": [],
            "location": {
                "latitude": None,
                "longitude": None,
                "address": {
                    "city": None,
                    "state": None,
                    "market": None
                }
            },
            "details": {
                "price_range": None,
                "sqft_range": None,
                "bed_range": None,
                "bath_range": None,
                "stories_range": None,
                "community_count": 1
            },
            "amenities": [],
            "homeplans": [],
            "homesites": [],
            "nearbyplaces": [],
            "collections": []
        }

        # Extract community name
        name_elem = soup.find('div', class_='community_listing_details').find('h1')
        if name_elem:
            # Clean up the name by removing extra whitespace and newlines
            name_parts = [part.strip() for part in name_elem.text.split('\n') if part.strip()]
            data["name"] = name_parts[0]  # Take only the first part as the name
            logger.info(f"Found community name: {data['name']}")

        # Extract price from and price range
        price_elem = soup.find('span', class_='price')
        if price_elem:
            price_text = price_elem.text.strip()
            price = extract_price(price_text)
            if price:
                data["price_from"] = f"From {price}"
                data["details"]["price_range"] = data["price_from"]
                logger.info(f"Found price: {data['price_from']}")

        # Extract address and get coordinates
        address_elem = soup.find('p', class_='community-page-address')
        if address_elem:
            # Clean up the address by removing extra whitespace, newlines and commas
            address_parts = [part.strip() for part in address_elem.text.split('\n') if part.strip()]
            data["address"] = ', '.join(address_parts)
            # Parse city and state from address
            if len(address_parts) >= 2:
                city_state = address_parts[-1].split(',')
                if len(city_state) >= 2:
                    data["location"]["address"]["city"] = city_state[0].strip()
                    state_zip = city_state[1].strip().split()
                    if len(state_zip) >= 1:
                        data["location"]["address"]["state"] = state_zip[0]
                    data["location"]["address"]["market"] = "Atlanta Metro"
            
            # Try to find coordinates in the page source first
            script_tags = soup.find_all('script')
            coordinates_found = False
            for script in script_tags:
                if script.string:
                    # Look for latitude and longitude in script content
                    lat_match = re.search(r'latitude["\s:]+([0-9.-]+)', script.string)
                    lon_match = re.search(r'longitude["\s:]+([0-9.-]+)', script.string)
                    if lat_match and lon_match:
                        lat = float(lat_match.group(1))
                        lon = float(lon_match.group(1))
                        data["location"]["latitude"] = lat
                        data["location"]["longitude"] = lon
                        logger.info(f"Found coordinates in page source: {lat}, {lon}")
                        coordinates_found = True
                        break
            
            # If no coordinates found in page source, try map element
            if not coordinates_found:
                map_div = soup.find('div', {'data-lat': True, 'data-lng': True})
                if map_div:
                    lat = float(map_div.get('data-lat'))
                    lon = float(map_div.get('data-lng'))
                    data["location"]["latitude"] = lat
                    data["location"]["longitude"] = lon
                    logger.info(f"Found coordinates from map element: {lat}, {lon}")
                    coordinates_found = True
            
            # If still no coordinates, try geocoding as fallback
            if not coordinates_found:
                lat, lon = get_coordinates(data["address"])
                if lat and lon:
                    data["location"]["latitude"] = lat
                    data["location"]["longitude"] = lon
                    logger.info(f"Found coordinates from geocoding: {lat}, {lon}")
            
            logger.info(f"Found address: {data['address']}")

        # Extract phone
        phone_elem = soup.find('a', class_='cells phone')
        if phone_elem:
            data["phone"] = phone_elem.find('span').text.strip()
            logger.info(f"Found phone: {data['phone']}")

        # Extract description
        desc_text = []
        # Try to find description in the overview section
        overview_section = soup.find('section', class_='overview-communities-block')
        if overview_section:
            first_p = overview_section.find('p')
            if first_p:
                # Remove the "strong" tags and keep only the text
                desc_text = first_p.text.strip()
                # Remove any extra whitespace or newlines
                desc_text = ' '.join(desc_text.split())
                data["description"] = desc_text
                logger.info("Found description from overview section")
        
        # If no description found in overview section, try other sections as fallback
        if not data["description"]:
            desc_section = soup.find('div', class_='community-description')
            if desc_section and desc_section.find('p'):
                desc_text = desc_section.find('p').text.strip()
                data["description"] = ' '.join(desc_text.split())
                logger.info("Found description from community description section")

        # Extract main image (just one)
        image_container = soup.find('div', class_='carousel')
        main_image = get_first_valid_image(image_container)
        if main_image:
            data["images"] = [main_image]
            logger.info("Found main image")

        # Extract home plans with driver for floor plan images
        data['homeplans'] = extract_homeplans(soup, driver)

        # Extract quick move-in homes
        qmi_containers = soup.find_all('li', class_='floor_plan_contain card quick-move-in-card')
        sqft_values = []
        bed_values = []
        bath_values = []
        
        for qmi in qmi_containers:
            homesite = {
                "name": None,
                "plan": None,
                "id": None,
                "address": None,
                "price": None,
                "beds": None,
                "baths": None,
                "sqft": None,
                "status": "Move-in Ready",
                "image_url": None,
                "url": None,
                "latitude": data["location"]["latitude"],
                "longitude": data["location"]["longitude"],
                "overview": None,
                "images": []
            }
            
            # Extract address and name
            address_elem = qmi.find('h3', class_='street-number')
            if address_elem:
                full_address = address_elem.text.strip().split('|')[0].strip()
                homesite["address"] = f"{full_address}, McDonough, GA"
                homesite["name"] = full_address
            
            # Extract plan name
            plan_elem = qmi.find('span', class_='title')
            if plan_elem:
                homesite["plan"] = plan_elem.text.strip()
            
            # Extract price
            price_elem = qmi.find('span', class_='price')
            if price_elem:
                homesite["price"] = price_elem.text.strip()
            
            # Extract specs
            beds_elem = qmi.find('img', alt='Bedrooms')
            if beds_elem:
                beds = beds_elem.find_next('span').text.strip()
                beds_num = beds.split()[0]  # Get just the number
                homesite["beds"] = beds_num
                bed_values.append(int(beds_num))
                
            baths_elem = qmi.find('img', alt='Bathrooms')
            if baths_elem:
                baths = baths_elem.find_next('span').text.strip()
                baths_num = baths.split()[0]  # Get just the number
                homesite["baths"] = baths_num
                bath_values.append(float(baths_num))
                
            sqft_elem = qmi.find('img', alt='Square Footage')
            if sqft_elem:
                sqft = sqft_elem.find_next('span').text.strip()
                sqft_num = sqft.split()[0].replace(',', '')  # Get just the number
                homesite["sqft"] = sqft_num
                sqft_values.append(int(sqft_num))
            
            # Extract image
            img_elem = qmi.find('img', class_='js-img')
            if img_elem and img_elem.get('src'):
                img_url = img_elem['src']
                if not img_url.startswith('http'):
                    img_url = 'https://www.centurycommunities.com' + img_url
                homesite["image_url"] = img_url
                homesite["images"] = [img_url]
            
            # Extract URL and get images from detail page
            url_elem = qmi.find('a', class_='btn btn-primary')
            if url_elem:
                url = url_elem['href']
                if not url.startswith('http'):
                    url = 'https://www.centurycommunities.com' + url
                homesite["url"] = url
                
                # Get images from detail page
                homesite["images"] = get_homesite_images(driver, url)
            
            # Extract ID from URL
            if url:
                lot_id = url.split('/')[-2].split('---')[0]
                homesite["id"] = lot_id
            
            # Extract overview from flags
            flags = qmi.find_all('div', class_=['custom-flag1-icon', 'custom-flag2-icon'])
            if flags:
                overview = ' '.join(flag.text.strip() for flag in flags)
                homesite["overview"] = overview
            
            data["homesites"].append(homesite)
            logger.info(f"Added homesite: {homesite['name']}")
            
        # Update details ranges
        if sqft_values:
            min_sqft = min(sqft_values)
            max_sqft = max(sqft_values)
            data["details"]["sqft_range"] = f"{min_sqft:,} - {max_sqft:,} sq ft" if min_sqft != max_sqft else f"{min_sqft:,} sq ft"
            
        if bed_values:
            min_beds = min(bed_values)
            max_beds = max(bed_values)
            data["details"]["bed_range"] = f"{min_beds} - {max_beds} Beds" if min_beds != max_beds else f"{min_beds} Beds"
            
        if bath_values:
            min_baths = min(bath_values)
            max_baths = max(bath_values)
            data["details"]["bath_range"] = f"{min_baths} - {max_baths} Baths" if min_baths != max_baths else f"{min_baths} Baths"
            
        # Set stories range based on sqft (assuming 1000-1500 sqft per story as a rough estimate)
        if sqft_values:
            max_sqft = max(sqft_values)
            stories = 2 if max_sqft > 2000 else 1  # Most homes over 2000 sqft are 2 story
            data["details"]["stories_range"] = f"{stories} Story"

        # Extract nearby places
        nearby_section = soup.find('section', class_='communities-block')
        if nearby_section:
            # Process each category section (Schools, Shopping, Dining)
            for section in nearby_section.find_all('section', class_=['schoolratings', 'col-sm-12']):
                heading = section.find('h3')
                if heading:
                    category_name = heading.find('span').text.strip()
                    description_div = section.find('div', class_='description')
                    if description_div:
                        for place in description_div.find_all('p'):
                            place_name = place.text.strip()
                            if place_name and place_name != '\xa0':  # Skip empty entries
                                nearby = {
                                    "name": place_name,
                                    "category": category_name,
                                    "distance": None,  # Not provided in the HTML
                                    "rating": None,    # Not provided in the HTML
                                    "reviews": None    # Not provided in the HTML
                                }
                                data["nearbyplaces"].append(nearby)
            logger.info(f"Found {len(data['nearbyplaces'])} nearby places")

        # Save JSON
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Data saved to: {json_file}")

        return data

    except Exception as e:
        logger.error(f"Error processing page: {str(e)}")
        return None
    finally:
        if driver:
            try:
                driver.quit()
            except Exception as e:
                logger.error(f"Error closing driver: {str(e)}")

def main():
    """Main function"""
    try:
        # Parse command line arguments
        parser = argparse.ArgumentParser(description='Scrape Century Communities pages')
        parser.add_argument('--url', help='Process a single URL')
        parser.add_argument('--batch', action='store_true', help='Process all URLs from centurycommunities_links.json')
        args = parser.parse_args()

        # Ensure output directories exist
        output_dir = 'data/centurycommunities'
        os.makedirs(f'{output_dir}/html', exist_ok=True)
        os.makedirs(f'{output_dir}/json', exist_ok=True)
        
        if args.batch:
            try:
                # Look for century_links.json in several possible locations
                possible_paths = [
                    'centurycommunities_links.json',
                    'data/centurycommunities_links.json',
                    '../centurycommunities_links.json',
                    os.path.join(os.path.dirname(__file__), 'century_links.json')
                ]
                
                json_file = None
                for path in possible_paths:
                    if os.path.exists(path):
                        json_file = path
                        logger.info(f"Found century_links.json at: {path}")
                        break
                
                if not json_file:
                    logger.error("Could not find century_links.json in any expected location")
                    return
                
                # Read URLs from century_links.json
                with open(json_file, 'r', encoding='utf-8') as f:
                    urls = json.load(f)
                
                if not urls:
                    logger.error("No URLs found in century_links.json")
                    return
                
                logger.info(f"Found {len(urls)} URLs to process")
                
                # Process each URL
                for i, url in enumerate(urls, 1):
                    try:
                        logger.info(f"Processing URL {i}/{len(urls)}")
                        fetch_page(url, output_dir)
                        time.sleep(2)  # Add delay to avoid too frequent requests
                    except Exception as e:
                        logger.error(f"Failed to process URL {url}: {str(e)}")
                        continue
                        
            except Exception as e:
                logger.error(f"Error during batch processing: {str(e)}")
                logger.exception("Detailed error information:")
                return
                
        elif args.url:
            # Process specified URL
            fetch_page(args.url, output_dir)
        else:
            # Process default URL
            default_url = "https://www.centurycommunities.com/find-your-new-home/georgia/atlanta-metro/mcdonough2/oakhurst-manor/"
            fetch_page(default_url, output_dir)
        
    except Exception as e:
        logger.error(f"Main program execution error: {str(e)}")
        logger.exception("Detailed error information:")

if __name__ == "__main__":
    main() 